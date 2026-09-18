#!/usr/bin/env python3
"""unity_pack -- emit a packed engine.c + data.c from a Unity-subset project.

See UNITY_PACK.md. Walks scripts and scenes, keeps only the Unity API that
is called, and lays objects out as small as the data allows: drop z in 2D,
float16 for static backgrounds, bitfields for small ints, and uint8_t
indices instead of pointers when a class is bounded (hand-placed, never
spawned, N ≤ 256).

Does not invent scene assets (ParticleSystem pools, AnimationCurves,
InputAction maps). Runtime `AddComponent<T>` is supported for packed
builtins and authored MonoBehaviours. Types with
`DisallowMultipleComponent` (all packed builtins; scripts that declare
the attribute) refuse a second add and print Unity's error; others
GetOrAdd into a pre-sized pool.

    python3 tools/unity_pack.py <project> -o <outdir>
"""

from __future__ import annotations

import os
import re
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cs2cpp as cs2cpp  # noqa: E402


class PackError(Exception):
    def __init__(self, message):
        self.message = message
        Exception.__init__(self, message)


def _assets_rel_path(path):
    """Unity-style path: `Assets/...` when under an Assets tree."""
    norm = path.replace("\\", "/")
    i = norm.find("/Assets/")
    if i >= 0:
        return norm[i + 1:]
    if norm.startswith("Assets/"):
        return norm
    return os.path.basename(path) if path else "<cs>"


def _check_csharp_lex(path, text):
    """Refuse spellings Unity/csc reject before any rewrite.

    C# real-literals need digits after `.` (`0.0f`) or a bare suffix (`0f`).
    C++-style `0.f` lexes as integer `0`, member access `.`, identifier `f`
    → CS1061. Catch it here so diagnostics stay against C# source.
    """
    scan = cs2cpp._blank(text)
    for m in re.finditer(r"(?<![\w.])\d+\.([fFdDmM])\b", scan):
        suffix = m.group(1)
        idx = m.start(1)
        line = text.count("\n", 0, idx) + 1
        col = idx - (text.rfind("\n", 0, idx) + 1) + 1
        raise PackError(
            "%s(%d,%d): error CS1061: 'int' does not contain a definition "
            "for '%s' and no accessible extension method '%s' accepting a "
            "first argument of type 'int' could be found (are you missing a "
            "using directive or an assembly reference?)"
            % (_assets_rel_path(path), line, col, suffix, suffix)
        )
    # transform.position is Vector3; += Vector2 is ambiguous (CS0034).
    # Assignment `= new Vector2(...)` is fine via Vector2→Vector3 implicit.
    for m in re.finditer(
            r"transform\.position\s*(?:\+=|-=)\s*new\s+Vector2\b", scan):
        idx = m.start()
        line = text.count("\n", 0, idx) + 1
        col = idx - (text.rfind("\n", 0, idx) + 1) + 1
        raise PackError(
            "%s(%d,%d): error CS0034: Operator '%s' is ambiguous on "
            "operands of type 'Vector3' and 'Vector2'"
            % (_assets_rel_path(path), line, col,
               "+=" if "+=" in m.group(0) else "-=")
        )


# Built-in Unity components AddComponent may create at runtime.
_ADDABLE_BUILTINS = frozenset((
    "Camera",
    "Light",
    "SpriteRenderer",
    "Rigidbody2D",
    "Rigidbody",
    "BoxCollider2D",
    "CircleCollider2D",
    "BoxCollider",
    "SphereCollider",
    "Animation",
    "Animator",
))

# Unity marks these with [DisallowMultipleComponent] — a second AddComponent
# logs an error and returns null instead of returning the existing instance.
_DISALLOW_MULTIPLE_BUILTINS = _ADDABLE_BUILTINS

# Types that still require inventing assets / systems — AddComponent refused.
_REFUSED_ADDCOMPONENT = frozenset((
    "ParticleSystem",
    "Canvas",
    "AudioSource",
))


def _progress(msg):
    """Incremental status for long packs (large scenes / many PNGs)."""
    sys.stderr.write("unity_pack: %s\n" % msg)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Unity / Godot API surface we are willing to emit
# ---------------------------------------------------------------------------

#: name -> snippet of C we emit only if a script mentions it
_API = {
    "Mathf.Abs": "static float Mathf_Abs(float f) { return f < 0.f ? -f : f; }",
    "Mathf.Min": "static float Mathf_Min(float a, float b) { return a < b ? a : b; }",
    "Mathf.Max": "static float Mathf_Max(float a, float b) { return a > b ? a : b; }",
    "Mathf.Clamp": (
        "static float Mathf_Clamp(float v, float lo, float hi) {\n"
        "    if (v < lo) return lo; if (v > hi) return hi; return v;\n}"
    ),
    "Mathf.Lerp": (
        "static float Mathf_Lerp(float a, float b, float t) {\n"
        "    if (t < 0.f) t = 0.f; if (t > 1.f) t = 1.f;\n"
        "    return a + (b - a) * t;\n}"
    ),
    "Mathf.Sin": "static float Mathf_Sin(float f) { return sinf(f); }",
    "Mathf.Cos": "static float Mathf_Cos(float f) { return cosf(f); }",
    "Time.deltaTime": None,  # globals in data.c — host can poke
    "Time.time": None,
    "Time.fixedDeltaTime": None,
    "Physics2D.gravity": None,
    "Physics.gravity": None,
    "Rigidbody2D": True,
    "Rigidbody": True,
    "RenderSettings.ambientLight": None,
    "Camera.main": None,
    # Input Manager (legacy): host pokes floats/ints in data.c. Snippets
    # are emitted in emit_engine once string.h / externs are in place.
    "Input.GetAxis": True,
    "Input.GetButton": True,
    "Input.GetKey": True,
    "Keyboard.current": True,
    # Player.log by default (not stdout). -logFile - → stdout.
    "Debug.Log": True,
    "print": True,
    # Terminal / stdout — System.Console, not Debug.Log.
    "Console.WriteLine": True,
    "GameObject.Find": True,
    "GetComponent": True,
}

# APIs that would require inventing scene components / assets we do not pack.
_REFUSED_API = {
    "ParticleSystem.Emit": (
        "ParticleSystem is a Unity component — unity_pack does not invent "
        "particle pools. Keep particles in the authored project, or drive "
        "motion from packed MonoBehaviour fields only."
    ),
    "AnimationCurve.Evaluate": (
        "AnimationCurve assets are not imported — unity_pack does not invent "
        "default curves. Animate with Time / Mathf on packed fields, or wait "
        "for curve import."
    ),
    "InputAction": (
        "Unity Input System InputAction assets are not imported — unity_pack "
        "does not invent action maps. Use Input.GetAxis / GetButton or "
        "Keyboard.current with a host, or wait for action-asset import."
    ),
    "Keyboard": (
        "Keyboard is UnityEngine.InputSystem.Keyboard — add "
        "`using UnityEngine.InputSystem;` or qualify the type. "
        "unity_pack does not invent a global Keyboard alias."
    ),
    "Console": (
        "Console is System.Console — add `using System;` or qualify "
        "System.Console.WriteLine. Debug.Log / print go to Player.log, not "
        "the terminal."
    ),
    "Gamepad.current": (
        "Unity Input System Gamepad.current needs the Input System package "
        "runtime — unity_pack does not invent device graphs."
    ),
    "UnityEngine.UI": (
        "uGUI (Canvas / Text / Image) is not invented by the packer. Keep UI "
        "in the authored Unity project, or wait for Canvas import."
    ),
    "Canvas": (
        "Canvas is a Unity component — unity_pack does not invent UI roots."
    ),
}

_SPAWN = re.compile(
    r"(?<![\w.])(Instantiate|Destroy|Object\.Instantiate|"
    r"GameObject\.Instantiate|new\s+GameObject)\b"
)
_VEC3Z = re.compile(r"\.(z)\b|Vector3|Quaternion")
_UNITY_API = re.compile(
    r"(?:AddComponent\s*<\s*[\w.]+\s*>|"
    r"(?<![\w])(?:Mathf\.(?:Abs|Min|Max|Clamp|Lerp|Sin|Cos)|"
    r"Time\.(?:deltaTime|time|fixedDeltaTime)|"
    r"Input\.(?:GetAxis|GetButton|GetKey)|"
    r"RenderSettings\.ambientLight|Camera\.main|"
    r"transform\.position|Physics2D\.gravity|Physics\.gravity|"
    r"Rigidbody2D|Rigidbody|"
    r"ParticleSystem\.Emit|"
    r"AnimationCurve\.Evaluate|"
    r"InputAction|Keyboard\.current|Gamepad\.current|"
    r"UnityEngine\.UI|"
    r"(?<![.\w])Canvas(?=\s|\.|;)|"
    r"Debug\.Log|(?<![\w.])print(?=\s*\()|"
    r"System\.Console\.WriteLine|(?<![\w.])Console\.WriteLine|"
    r"GameObject\.Find|GetComponent\s*<|"
    r"Vector2|Vector3|Quaternion)\b)"
)
_WANT_INPUT = frozenset({"Input.GetAxis", "Input.GetButton", "Input.GetKey"})
_KEYBOARD_KEY = re.compile(
    r"Keyboard\.current\.(\w+)Key\.(isPressed|wasPressedThisFrame|"
    r"wasReleasedThisFrame)"
)


# ---------------------------------------------------------------------------
# Project walk
# ---------------------------------------------------------------------------

def _c_string(s):
    """Quote a Python str as a C string literal."""
    return '"%s"' % (
        s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        .replace("\r", "\\r").replace("\0", "\\0")
    )


def _yaml_scalar(raw):
    """Strip a simple Unity YAML scalar (optional quotes)."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s


def player_identity(root):
    """companyName / productName from ProjectSettings, else Unity-ish defaults.

    Defaults: company DefaultCompany, product = project folder name — same
    fallback Unity uses when settings are unset.
    """
    company = "DefaultCompany"
    product = os.path.basename(os.path.abspath(root).rstrip(os.sep)) or "Player"
    settings = os.path.join(root, "ProjectSettings", "ProjectSettings.asset")
    if os.path.isfile(settings):
        text = _read(settings)
        m = re.search(r"(?m)^\s*companyName:\s*(.*)$", text)
        if m and m.group(1).strip():
            company = _yaml_scalar(m.group(1)) or company
        m = re.search(r"(?m)^\s*productName:\s*(.*)$", text)
        if m and m.group(1).strip():
            product = _yaml_scalar(m.group(1)) or product
    return company, product


def unity_player_log_path(company, product, home=None):
    """Host path matching Unity's Player.log layout for this OS."""
    if home is None:
        home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Logs", company, product,
                            "Player.log")
    if sys.platform.startswith("win"):
        base = os.environ.get("USERPROFILE") or home
        return os.path.join(base, "AppData", "LocalLow", company, product,
                            "Player.log")
    return os.path.join(home, ".config", "unity3d", company, product,
                        "Player.log")


def _read(path):
    with open(path) as f:
        return f.read()


def _walk_files(root, exts):
    out = []
    n_dirs = 0
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "Library", "Temp", "obj",
                                    "Builds", "Logs", "Build",
                                    "graphify-out", "__pycache__",
                                    "node_modules")]
        n_dirs += 1
        if n_dirs % 500 == 0:
            _progress("  walked %d dir(s), %d match(es) so far"
                       % (n_dirs, len(out)))
        for n in names:
            if any(n.endswith(e) for e in exts):
                out.append(os.path.join(dirpath, n))
    out.sort()
    return out


def _guid_map(root, asset_guids=None):
    """Unity .meta `guid:` next to a .cs file → script path.

    If *asset_guids* is provided (full guid→path map), derive script guids
    from it without a second tree walk.
    """
    out = {}
    if asset_guids is not None:
        for g, path in asset_guids.items():
            if path.lower().endswith(".cs"):
                out[g] = path
        _progress("script metas from asset map: %d" % len(out))
        return out
    _progress("walking project tree for .cs.meta files")
    metas = list(_walk_files(root, (".cs.meta",)))
    _progress("indexing %d script .meta file(s)" % len(metas))
    for i, meta in enumerate(metas):
        if metas and ((i + 1) % 50 == 0 or i + 1 == len(metas)):
            _progress("  script metas %d/%d" % (i + 1, len(metas)))
        text = _read(meta)
        m = re.search(r"(?m)^guid:\s*([0-9a-fA-F]+)\s*$", text)
        if not m:
            continue
        cs = meta[:-5] if meta.endswith(".meta") else meta
        out[m.group(1).lower()] = cs
    return out


def _asset_guid_map(root):
    """Any Unity .meta guid → asset path (scripts, textures, …)."""
    out = {}
    _progress("walking project tree for .meta files")
    metas = list(_walk_files(root, (".meta",)))
    _progress("indexing %d .meta file(s)" % len(metas))
    for i, meta in enumerate(metas):
        if metas and ((i + 1) % 200 == 0 or i + 1 == len(metas)):
            _progress("  asset metas %d/%d" % (i + 1, len(metas)))
        text = _read(meta)
        m = re.search(r"(?m)^guid:\s*([0-9a-fA-F]+)\s*$", text)
        if not m:
            continue
        asset = meta[:-5] if meta.endswith(".meta") else meta
        out[m.group(1).lower()] = asset
    return out


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _load_png_rgba(path):
    """Decode an 8-bit non-interlaced PNG to (w, h, rgba_bytes).

    Supports color types 2 (RGB) and 6 (RGBA). Used so editing a referenced
    sprite asset changes packed visuals — no invented placeholder colors.
    """
    import struct
    import zlib

    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PackError("not a PNG: %s" % path)
    pos = 8
    w = h = None
    color_type = None
    idat = []
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos = pos + 12 + length
        if tag == b"IHDR":
            w, h, bit_depth, color_type, comp, filt, inter = struct.unpack(
                ">IIBBBBB", chunk)
            if bit_depth != 8 or inter != 0 or comp != 0 or filt != 0:
                raise PackError(
                    "unsupported PNG (need 8-bit non-interlaced): %s" % path)
            if color_type not in (2, 6):
                raise PackError(
                    "unsupported PNG color type %d (need RGB/RGBA): %s"
                    % (color_type, path))
        elif tag == b"IDAT":
            idat.append(chunk)
        elif tag == b"IEND":
            break
    if w is None or not idat:
        raise PackError("incomplete PNG: %s" % path)
    bpp = 4 if color_type == 6 else 3
    raw = zlib.decompress(b"".join(idat))
    stride = w * bpp
    expect = (stride + 1) * h
    if len(raw) < expect:
        raise PackError("PNG IDAT too short: %s" % path)
    rows = []
    prev = bytearray(stride)
    off = 0
    for _y in range(h):
        ftype = raw[off]
        off += 1
        row = bytearray(raw[off:off + stride])
        off += stride
        if ftype == 0:
            pass
        elif ftype == 1:  # Sub
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + left) & 255
        elif ftype == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 255
        elif ftype == 3:  # Average
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) // 2)) & 255
        elif ftype == 4:  # Paeth
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                up = prev[i]
                ul = prev[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth(left, up, ul)) & 255
        else:
            raise PackError("bad PNG filter %d in %s" % (ftype, path))
        rows.append(bytes(row))
        prev = row
    # PNG stores top row first; OpenGL / Unity sprite UVs treat the first
    # texel row as the bottom. Flip so authored art is not Y-mirrored.
    rows.reverse()
    if color_type == 6:
        rgba = b"".join(rows)
    else:
        out = bytearray(w * h * 4)
        i = 0
        for row in rows:
            for x in range(w):
                o = x * 3
                out[i] = row[o]
                out[i + 1] = row[o + 1]
                out[i + 2] = row[o + 2]
                out[i + 3] = 255
                i += 4
        rgba = bytes(out)
    return w, h, rgba


def _pixels_per_unit(asset_path):
    """Unity TextureImporter `spritePixelsToUnits` (Sprite.pixelsPerUnit).

    Default 100 matches Unity when the .meta omits the field.
    """
    meta = asset_path + ".meta"
    try:
        text = _read(meta)
    except IOError:
        return 100.0
    m = re.search(r"(?m)^\s*spritePixelsToUnits:\s*([0-9.]+)\s*$", text)
    if not m:
        return 100.0
    v = float(m.group(1))
    return v if v > 0.0 else 100.0


def _quat_rotate_vec(qx, qy, qz, qw, vx, vy, vz):
    """Apply Unity quaternion (x,y,z,w) to a vector."""
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return (
        vx + qw * tx + (qy * tz - qz * ty),
        vy + qw * ty + (qz * tx - qx * tz),
        vz + qw * tz + (qx * ty - qy * tx),
    )


def _quat_mul(a, b):
    """Hamilton product a*b for Unity quaternions (x,y,z,w)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _quat_z_rad(qx, qy, qz, qw):
    """Planar angle (radians) of local +X after *rot* — SpriteRenderer Z spin."""
    rx, ry, _rz = _quat_rotate_vec(qx, qy, qz, qw, 1.0, 0.0, 0.0)
    return math.atan2(ry, rx)


# Unity defaults when Collider/Rigidbody m_Material is {fileID: 0}.
_DEFAULT_MAT2D = {
    "friction": 0.4,
    "bounciness": 0.0,
    "friction_combine": 0,  # Average
    "bounce_combine": 0,
}
_DEFAULT_MAT3D = {
    "dynamic_friction": 0.6,
    "static_friction": 0.6,
    "bounciness": 0.0,
    "friction_combine": 0,
    "bounce_combine": 0,
}


def _parse_material_guid(block):
    """m_Material: {fileID: 0} or {fileID: 6200000, guid: …, type: 2}."""
    m = re.search(
        r"(?m)^\s+m_Material:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*"
        r"([0-9a-fA-F]+))?",
        block)
    if not m:
        return None
    if m.group(1) == "0" or not m.group(2):
        return None
    return m.group(2).lower()


def _parse_physics_material2d(text):
    fr = re.search(r"(?m)^\s+friction:\s*([0-9.eE+-]+)", text)
    bn = re.search(r"(?m)^\s+bounciness:\s*([0-9.eE+-]+)", text)
    fc = re.search(r"(?m)^\s+m_FrictionCombine:\s*(\d+)", text)
    bc = re.search(r"(?m)^\s+m_BounceCombine:\s*(\d+)", text)
    return {
        "friction": float(fr.group(1)) if fr else 0.4,
        "bounciness": float(bn.group(1)) if bn else 0.0,
        "friction_combine": int(fc.group(1)) if fc else 0,
        "bounce_combine": int(bc.group(1)) if bc else 0,
    }


def _parse_physic_material3d(text):
    df = re.search(r"(?m)^\s+dynamicFriction:\s*([0-9.eE+-]+)", text)
    sf = re.search(r"(?m)^\s+staticFriction:\s*([0-9.eE+-]+)", text)
    bn = re.search(r"(?m)^\s+bounciness:\s*([0-9.eE+-]+)", text)
    fc = re.search(r"(?m)^\s+frictionCombine:\s*(\d+)", text)
    bc = re.search(r"(?m)^\s+bounceCombine:\s*(\d+)", text)
    return {
        "dynamic_friction": float(df.group(1)) if df else 0.6,
        "static_friction": float(sf.group(1)) if sf else 0.6,
        "bounciness": float(bn.group(1)) if bn else 0.0,
        "friction_combine": int(fc.group(1)) if fc else 0,
        "bounce_combine": int(bc.group(1)) if bc else 0,
    }


def _load_physics_materials(asset_guids):
    """guid → PhysicsMaterial2D / PhysicMaterial from project assets."""
    mats2d = {}
    mats3d = {}
    for guid, path in (asset_guids or {}).items():
        low = path.lower()
        try:
            if low.endswith(".physicsmaterial2d"):
                mats2d[guid.lower()] = _parse_physics_material2d(_read(path))
            elif low.endswith(".physicmaterial"):
                mats3d[guid.lower()] = _parse_physic_material3d(_read(path))
        except (IOError, OSError):
            continue
    return mats2d, mats3d


def _resolve_mat2d(col_guid, rb_guid, mats2d):
    """Collider material, else Rigidbody2D material, else Unity 2D defaults."""
    if col_guid and col_guid in mats2d:
        return mats2d[col_guid]
    if rb_guid and rb_guid in mats2d:
        return mats2d[rb_guid]
    return dict(_DEFAULT_MAT2D)


def _resolve_mat3d(col_guid, rb_guid, mats3d):
    if col_guid and col_guid in mats3d:
        return mats3d[col_guid]
    if rb_guid and rb_guid in mats3d:
        return mats3d[rb_guid]
    return dict(_DEFAULT_MAT3D)


# ---------------------------------------------------------------------------
# AnimationClip / AnimatorController (authored assets)
# ---------------------------------------------------------------------------

def _parse_vec3_keyframes(curve_text):
    """Extract (time, x, y, z) keys from an AnimationClip Vector3 curve."""
    keys = []
    for m in re.finditer(
            r"time:\s*([0-9.eE+-]+)\s*\n\s*value:\s*\{x:\s*([^,}]+),\s*y:\s*"
            r"([^,}]+),\s*z:\s*([^}]+)\}",
            curve_text):
        keys.append((
            float(m.group(1)),
            float(m.group(2)),
            float(m.group(3)),
            float(m.group(4)),
        ))
    keys.sort(key=lambda k: k[0])
    return keys


def _parse_float_keyframes(curve_text):
    keys = []
    for m in re.finditer(
            r"time:\s*([0-9.eE+-]+)\s*\n\s*value:\s*([0-9.eE+-]+)",
            curve_text):
        keys.append((float(m.group(1)), float(m.group(2))))
    keys.sort(key=lambda k: k[0])
    return keys


def _curve_path_is_root(path_line):
    """Unity root curves use `path:` empty or `path: \"\"`."""
    if path_line is None:
        return True
    v = path_line.strip()
    return v == "" or v == '""'


def _parse_animation_clip(text):
    """Authored .anim → length, loop, root position/euler/scale keyframes."""
    nm = re.search(r"(?m)^\s+m_Name:\s*(.+)$", text)
    stop = re.search(r"(?m)^\s+m_StopTime:\s*([0-9.eE+-]+)", text)
    loop = re.search(r"(?m)^\s+m_LoopTime:\s*(\d+)", text)
    wrap = re.search(r"(?m)^\s+m_WrapMode:\s*(\d+)", text)
    # WrapMode 2 = Loop; LoopTime 1 also loops.
    do_loop = 1
    if loop:
        do_loop = int(loop.group(1))
    elif wrap and int(wrap.group(1)) == 2:
        do_loop = 1
    elif wrap:
        do_loop = 0

    def _root_vec3_curves(section_name):
        out = []
        # Each list entry: "- curve:" … "path: …"
        pat = re.compile(
            r"(?ms)^  - curve:\n(.*?)(?=^  - curve:|^  m_|\Z)")
        # Find section body after m_PositionCurves: etc.
        sm = re.search(
            r"(?ms)^  %s:\s*\n(.*?)(?=^  m_[A-Z]|\Z)" % section_name, text)
        if not sm:
            # empty list form: m_PositionCurves: []
            return out
        body = sm.group(1)
        if body.strip().startswith("[]"):
            return out
        for cm in re.finditer(
                r"(?ms)^  - curve:\n(.*?)(?=^  - curve:|^  m_|\Z)", body):
            block = cm.group(1)
            pm = re.search(r"(?m)^\s+path:\s*(.*)$", block)
            path = pm.group(1) if pm else ""
            if not _curve_path_is_root(path):
                continue
            keys = _parse_vec3_keyframes(block)
            if keys:
                out.extend(keys)
        return out

    pos = _root_vec3_curves("m_PositionCurves")
    euler = _root_vec3_curves("m_EulerCurves")
    scale = _root_vec3_curves("m_ScaleCurves")
    length = float(stop.group(1)) if stop else 0.0
    if length <= 0.0:
        for keys in (pos, euler, scale):
            if keys:
                length = max(length, keys[-1][0])
        if length <= 0.0:
            length = 1.0
    return {
        "name": (nm.group(1).strip() if nm else "Clip"),
        "length": length,
        "loop": do_loop,
        "pos_keys": pos,
        "euler_keys": euler,
        "scale_keys": scale,
    }


def _parse_animator_controller_default_clip(text):
    """Return motion clip guid of the default AnimatorState, or None."""
    dm = re.search(
        r"(?m)^\s+m_DefaultState:\s*\{fileID:\s*(-?\d+)\}", text)
    if not dm:
        return None
    default_id = dm.group(1)
    # Find AnimatorState block with that fileID and its m_Motion guid.
    for m in re.finditer(
            r"(?ms)^--- !u!1102 &(-?\d+)\n(.*?)(?=^--- |\Z)", text):
        if m.group(1) != default_id:
            continue
        block = m.group(2)
        gm = re.search(
            r"m_Motion:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*"
            r"([0-9a-fA-F]+))?",
            block)
        if gm and gm.group(2):
            return gm.group(2).lower()
        return None
    return None


def _load_animation_assets(asset_guids):
    """guid → AnimationClip dict; guid → default clip guid for controllers."""
    clips = {}
    controllers = {}
    for guid, path in (asset_guids or {}).items():
        low = path.lower()
        try:
            if low.endswith(".anim"):
                clips[guid.lower()] = _parse_animation_clip(_read(path))
            elif low.endswith(".controller"):
                controllers[guid.lower()] = _parse_animator_controller_default_clip(
                    _read(path))
        except (IOError, OSError):
            continue
    return clips, controllers


def _parse_asset_guid_ref(block, field):
    """m_Field: {fileID: N, guid: …, type: 2} → guid or None."""
    m = re.search(
        r"(?m)^\s+%s:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*"
        r"([0-9a-fA-F]+))?" % re.escape(field),
        block)
    if not m or m.group(1) == "0" or not m.group(2):
        return None
    return m.group(2).lower()


def _resolve_world_trs(xf_id, by_id, cache=None, stack=None):
    """Compose local TRS up m_Father / m_TransformParent into world TRS."""
    if cache is None:
        cache = {}
    if stack is None:
        stack = set()
    xf_id = str(xf_id)
    if xf_id in cache:
        return cache[xf_id]
    xf = by_id.get(xf_id)
    if not xf:
        ident = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
        cache[xf_id] = ident
        return ident
    if xf_id in stack:
        # Cycle — treat as root local.
        pos = xf.get("pos") or (0.0, 0.0, 0.0)
        rot = xf.get("rot") or (0.0, 0.0, 0.0, 1.0)
        scale = xf.get("scale") or (1.0, 1.0, 1.0)
        out = (pos, rot, scale)
        cache[xf_id] = out
        return out
    stack.add(xf_id)
    pos = xf.get("pos") or (0.0, 0.0, 0.0)
    rot = xf.get("rot") or (0.0, 0.0, 0.0, 1.0)
    scale = xf.get("scale") or (1.0, 1.0, 1.0)
    father = xf.get("father_id")
    if not father or father == "0" or father not in by_id:
        out = (pos, rot, scale)
    else:
        pp, pr, ps = _resolve_world_trs(father, by_id, cache, stack)
        lx = float(pos[0]) * float(ps[0])
        ly = float(pos[1]) * float(ps[1])
        lz = float(pos[2]) * float(ps[2])
        wx, wy, wz = _quat_rotate_vec(
            pr[0], pr[1], pr[2], pr[3], lx, ly, lz)
        out = (
            (float(pp[0]) + wx, float(pp[1]) + wy, float(pp[2]) + wz),
            _quat_mul(pr, rot),
            (float(ps[0]) * float(scale[0]),
             float(ps[1]) * float(scale[1]),
             float(ps[2]) * float(scale[2])),
        )
    stack.discard(xf_id)
    cache[xf_id] = out
    return out


def _attach_sprite_textures(objects, asset_guids):
    """Load PNG pixels for each SpriteRenderer that references a project sprite.

    World half-extents follow Unity: (pixels / pixelsPerUnit) * scale / 2.
    PNG decode is cached by path so shared sprites are not re-decoded.
    """
    todo = [o for o in objects if o.get("sprite")]
    n = len(todo)
    cache = {}  # path -> (w, h, rgba, ppu) or None if unloadable
    if n:
        _progress("loading sprites for %d SpriteRenderer(s)" % n)
    for i, o in enumerate(todo):
        if n >= 8 and ((i + 1) % 100 == 0 or i + 1 == n):
            _progress("  sprites %d/%d (%d unique PNG(s))" % (
                i + 1, n, len(cache)))
        sp = o.get("sprite")
        if not sp:
            continue
        path = asset_guids.get(sp.get("sprite_guid") or "")
        if not path or not path.lower().endswith(".png"):
            o["sprite"] = None
            continue
        if path not in cache:
            try:
                w, h, rgba = _load_png_rgba(path)
                cache[path] = (w, h, rgba, _pixels_per_unit(path))
            except (PackError, IOError):
                cache[path] = None
        hit = cache[path]
        if hit is None:
            o["sprite"] = None
            continue
        w, h, rgba, ppu = hit
        sx = abs(float(sp.get("scale_x", 1.0)))
        sy = abs(float(sp.get("scale_y", 1.0)))
        sp["tex_path"] = path
        sp["tex_w"] = w
        sp["tex_h"] = h
        sp["tex_rgba"] = rgba
        sp["pixels_per_unit"] = ppu
        sp["half_w"] = (float(w) / ppu) * sx * 0.5
        sp["half_h"] = (float(h) / ppu) * sy * 0.5


def _collect_textures(objects):
    """Deduplicate sprite PNGs → plan texture table; set tex_id on sprites."""
    textures = []
    by_guid = {}
    for o in objects:
        sp = o.get("sprite")
        if not sp or "tex_rgba" not in sp:
            continue
        g = sp["sprite_guid"]
        if g not in by_guid:
            by_guid[g] = len(textures)
            textures.append({
                "guid": g,
                "path": sp["tex_path"],
                "w": sp["tex_w"],
                "h": sp["tex_h"],
                "rgba": sp["tex_rgba"],
            })
        sp["tex_id"] = by_guid[g]
    return textures


# ---------------------------------------------------------------------------
# Scene importers
# ---------------------------------------------------------------------------

def parse_unity_yaml(text, guid_to_script=None, asset_guids=None):
    """A Unity .unity YAML subset: GameObject + Transform + MonoBehaviour.

    Also imports authored Camera (!u!20), SpriteRenderer (!u!212),
    Rigidbody2D (!u!50), Rigidbody (!u!54), BoxCollider2D (!u!61),
    CircleCollider2D (!u!58), BoxCollider (!u!65), SphereCollider (!u!135),
    Animation (!u!111), Animator (!u!95), PhysicsMaterial2D / PhysicMaterial,
    and AnimationClip / AnimatorController assets. Does not invent any of
    those — missing components stay missing. Returns
    (objects, lights, cameras).
    """
    guid_to_script = guid_to_script or {}
    asset_guids = asset_guids or {}
    mats2d, mats3d = _load_physics_materials(asset_guids)
    anim_clips, anim_controllers = _load_animation_assets(asset_guids)
    objects = []
    lights = []
    cameras = []
    blocks = re.split(r"(?m)^---\s+", text)
    by_id = {}
    for block in blocks:
        hm = re.match(r"!u!(\d+)\s+&(\d+)", block)
        if not hm:
            continue
        type_id = hm.group(1)
        file_id = hm.group(2)
        kind = None
        km = re.search(
            r"(?m)^(GameObject|Transform|RectTransform|MonoBehaviour|"
            r"PrefabInstance|Light|Camera|SpriteRenderer|Rigidbody2D|"
            r"Rigidbody|BoxCollider2D|CircleCollider2D|BoxCollider|"
            r"SphereCollider|Animation|Animator):",
            block)
        if km:
            kind = km.group(1)
            if kind == "RectTransform":
                kind = "Transform"
        elif type_id == "108":
            kind = "Light"
        elif type_id == "20":
            kind = "Camera"
        elif type_id == "212":
            kind = "SpriteRenderer"
        elif type_id == "50":
            kind = "Rigidbody2D"
        elif type_id == "54":
            kind = "Rigidbody"
        elif type_id == "61":
            kind = "BoxCollider2D"
        elif type_id == "58":
            kind = "CircleCollider2D"
        elif type_id == "65":
            kind = "BoxCollider"
        elif type_id == "135":
            kind = "SphereCollider"
        elif type_id == "111":
            kind = "Animation"
        elif type_id == "95":
            kind = "Animator"
        elif type_id in ("4", "224"):
            kind = "Transform"
        rec = {"file_id": file_id, "kind": kind, "raw": block, "fields": {}}
        nm = re.search(r"(?m)^\s+m_Name:\s*(.+)$", block)
        if nm:
            rec["name"] = nm.group(1).strip()
        tag = re.search(r"(?m)^\s+m_TagString:\s*(.+)$", block)
        if tag:
            rec["tag"] = tag.group(1).strip()
        pos = re.search(
            r"m_LocalPosition:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
            r"\s*z:\s*([^}]+)\}", block)
        if pos:
            rec["pos"] = (float(pos.group(1)), float(pos.group(2)),
                          float(pos.group(3)))
        sc = re.search(
            r"m_LocalScale:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
            r"\s*z:\s*([^}]+)\}", block)
        if sc:
            rec["scale"] = (float(sc.group(1)), float(sc.group(2)),
                            float(sc.group(3)))
        rot = re.search(
            r"m_LocalRotation:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
            r"\s*z:\s*([^,}]+),\s*w:\s*([^}]+)\}", block)
        if rot:
            rec["rot"] = (float(rot.group(1)), float(rot.group(2)),
                          float(rot.group(3)), float(rot.group(4)))
        # Transform parent: m_Father. PrefabInstance: m_TransformParent.
        father = re.search(
            r"(?m)^\s+m_Father:\s*\{fileID:\s*(-?\d+)\}", block)
        if not father:
            father = re.search(
                r"(?m)^\s+m_TransformParent:\s*\{fileID:\s*(-?\d+)\}", block)
        if father:
            fid = father.group(1)
            if fid != "0":
                rec["father_id"] = fid
        # PrefabInstance nested form: m_Modification: … m_TransformParent:
        if kind == "PrefabInstance":
            tp = re.search(
                r"(?m)^\s+m_TransformParent:\s*\{fileID:\s*(-?\d+)\}", block)
            if tp and tp.group(1) != "0":
                rec["father_id"] = tp.group(1)
            # Apply common TRS overrides from m_Modifications.
            def _mod_f(axis_path):
                m = re.search(
                    r"propertyPath:\s*%s\s*\n\s*value:\s*([^\n]+)" % axis_path,
                    block)
                return float(m.group(1)) if m else None
            px, py, pz = (_mod_f("m_LocalPosition\\.x"),
                          _mod_f("m_LocalPosition\\.y"),
                          _mod_f("m_LocalPosition\\.z"))
            if px is not None or py is not None or pz is not None:
                rec["pos"] = (
                    px if px is not None else 0.0,
                    py if py is not None else 0.0,
                    pz if pz is not None else 0.0,
                )
            qx, qy, qz, qw = (_mod_f("m_LocalRotation\\.x"),
                              _mod_f("m_LocalRotation\\.y"),
                              _mod_f("m_LocalRotation\\.z"),
                              _mod_f("m_LocalRotation\\.w"))
            if qw is not None or qx is not None:
                rec["rot"] = (
                    qx if qx is not None else 0.0,
                    qy if qy is not None else 0.0,
                    qz if qz is not None else 0.0,
                    qw if qw is not None else 1.0,
                )
            sx, sy, sz = (_mod_f("m_LocalScale\\.x"),
                          _mod_f("m_LocalScale\\.y"),
                          _mod_f("m_LocalScale\\.z"))
            if sx is not None or sy is not None or sz is not None:
                rec["scale"] = (
                    sx if sx is not None else 1.0,
                    sy if sy is not None else 1.0,
                    sz if sz is not None else 1.0,
                )
        gm = re.search(r"guid:\s*([0-9a-fA-F]+)", block)
        if gm:
            rec["guid"] = gm.group(1).lower()
        for fm in re.finditer(r"(?m)^\s{2}(\w+):\s+(-?\d+(?:\.\d+)?)\s*$",
                              block):
            key = fm.group(1)
            if key.startswith("m_"):
                continue
            val = fm.group(2)
            rec["fields"][key] = float(val) if "." in val else int(val)
        if kind == "Light":
            inten = re.search(r"(?m)^\s+m_Intensity:\s*([0-9.eE+-]+)", block)
            col = re.search(
                r"m_Color:\s*\{r:\s*([^,}]+),\s*g:\s*([^,}]+),"
                r"\s*b:\s*([^,}]+)", block)
            lights.append({
                "file_id": file_id,
                "intensity": float(inten.group(1)) if inten else 1.0,
                "r": float(col.group(1)) if col else 1.0,
                "g": float(col.group(2)) if col else 1.0,
                "b": float(col.group(3)) if col else 1.0,
            })
        if kind == "SpriteRenderer":
            col = re.search(
                r"m_Color:\s*\{r:\s*([^,}]+),\s*g:\s*([^,}]+),"
                r"\s*b:\s*([^,}]+)", block)
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            # Unity null sprite is m_Sprite: {fileID: 0} — do not invent a draw.
            spr = re.search(
                r"m_Sprite:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*"
                r"([0-9a-fA-F]+))?",
                block)
            has_sprite = False
            if spr and int(spr.group(1)) != 0:
                g = spr.group(2).lower() if spr.group(2) else None
                # Must resolve to a project asset — no invent / dangling guid.
                has_sprite = bool(g and g in asset_guids)
            rec["sprite"] = {
                "r": float(col.group(1)) if col else 1.0,
                "g": float(col.group(2)) if col else 1.0,
                "b": float(col.group(3)) if col else 1.0,
                "enabled": int(en.group(1)) if en else 1,
                "has_sprite": has_sprite,
                "sprite_file_id": int(spr.group(1)) if spr else 0,
                "sprite_guid": (spr.group(2).lower()
                                if spr and spr.group(2) else None),
            }
        if kind == "Camera":
            ortho = re.search(r"(?m)^\s+orthographic:\s*(\d+)", block)
            osize = re.search(
                r"(?m)^\s+orthographic size:\s*([0-9.eE+-]+)", block)
            bg = re.search(
                r"m_BackGroundColor:\s*\{r:\s*([^,}]+),\s*g:\s*([^,}]+),"
                r"\s*b:\s*([^,}]+)", block)
            near = re.search(
                r"(?m)^\s+near clip plane:\s*([0-9.eE+-]+)", block)
            if not near:
                near = re.search(
                    r"(?m)^\s+m_NearClipPlane:\s*([0-9.eE+-]+)", block)
            far = re.search(
                r"(?m)^\s+far clip plane:\s*([0-9.eE+-]+)", block)
            if not far:
                far = re.search(
                    r"(?m)^\s+m_FarClipPlane:\s*([0-9.eE+-]+)", block)
            rec["camera"] = {
                "orthographic": int(ortho.group(1)) if ortho else 1,
                "orthographic_size": (
                    float(osize.group(1)) if osize else 5.0),
                "bg_r": float(bg.group(1)) if bg else 0.05,
                "bg_g": float(bg.group(2)) if bg else 0.05,
                "bg_b": float(bg.group(3)) if bg else 0.08,
                # Unity defaults when YAML omits clip planes.
                "near_clip": float(near.group(1)) if near else 0.3,
                "far_clip": float(far.group(1)) if far else 1000.0,
            }
        if kind == "Rigidbody2D":
            bt = re.search(r"(?m)^\s+m_BodyType:\s*(\d+)", block)
            mass = re.search(r"(?m)^\s+m_Mass:\s*([0-9.eE+-]+)", block)
            gs = re.search(r"(?m)^\s+m_GravityScale:\s*([0-9.eE+-]+)", block)
            vel = re.search(
                r"m_LinearVelocity:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}",
                block)
            # Older YAML used m_Velocity
            if not vel:
                vel = re.search(
                    r"m_Velocity:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}",
                    block)
            rec["rigidbody2d"] = {
                "body_type": int(bt.group(1)) if bt else 0,
                "mass": float(mass.group(1)) if mass else 1.0,
                "gravity_scale": float(gs.group(1)) if gs else 1.0,
                "vel_x": float(vel.group(1)) if vel else 0.0,
                "vel_y": float(vel.group(2)) if vel else 0.0,
                "material_guid": _parse_material_guid(block),
            }
        if kind == "Rigidbody":
            mass = re.search(r"(?m)^\s+m_Mass:\s*([0-9.eE+-]+)", block)
            ug = re.search(r"(?m)^\s+m_UseGravity:\s*(\d+)", block)
            vel = re.search(
                r"m_Velocity:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
                r"\s*z:\s*([^}]+)\}",
                block)
            # Newer Unity: m_LinearVelocity
            if not vel:
                vel = re.search(
                    r"m_LinearVelocity:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
                    r"\s*z:\s*([^}]+)\}",
                    block)
            rec["rigidbody"] = {
                "mass": float(mass.group(1)) if mass else 1.0,
                "use_gravity": int(ug.group(1)) if ug else 1,
                "vel_x": float(vel.group(1)) if vel else 0.0,
                "vel_y": float(vel.group(2)) if vel else 0.0,
                "vel_z": float(vel.group(3)) if vel else 0.0,
                "material_guid": _parse_material_guid(block),
            }
        if kind == "BoxCollider2D":
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            trig = re.search(r"(?m)^\s+m_IsTrigger:\s*(\d+)", block)
            off = re.search(
                r"m_Offset:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}", block)
            sz = re.search(
                r"m_Size:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}", block)
            rec["collider2d"] = {
                "kind": "box",
                "enabled": int(en.group(1)) if en else 1,
                "is_trigger": int(trig.group(1)) if trig else 0,
                "offset_x": float(off.group(1)) if off else 0.0,
                "offset_y": float(off.group(2)) if off else 0.0,
                "size_x": float(sz.group(1)) if sz else 1.0,
                "size_y": float(sz.group(2)) if sz else 1.0,
                "material_guid": _parse_material_guid(block),
            }
        if kind == "CircleCollider2D":
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            trig = re.search(r"(?m)^\s+m_IsTrigger:\s*(\d+)", block)
            off = re.search(
                r"m_Offset:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}", block)
            rad = re.search(r"(?m)^\s+m_Radius:\s*([0-9.eE+-]+)", block)
            rec["collider2d"] = {
                "kind": "circle",
                "enabled": int(en.group(1)) if en else 1,
                "is_trigger": int(trig.group(1)) if trig else 0,
                "offset_x": float(off.group(1)) if off else 0.0,
                "offset_y": float(off.group(2)) if off else 0.0,
                "radius": float(rad.group(1)) if rad else 0.5,
                "material_guid": _parse_material_guid(block),
            }
        if kind == "BoxCollider":
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            trig = re.search(r"(?m)^\s+m_IsTrigger:\s*(\d+)", block)
            center = re.search(
                r"m_Center:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
                r"\s*z:\s*([^}]+)\}", block)
            sz = re.search(
                r"m_Size:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
                r"\s*z:\s*([^}]+)\}", block)
            rec["collider3d"] = {
                "kind": "box",
                "enabled": int(en.group(1)) if en else 1,
                "is_trigger": int(trig.group(1)) if trig else 0,
                "offset_x": float(center.group(1)) if center else 0.0,
                "offset_y": float(center.group(2)) if center else 0.0,
                "offset_z": float(center.group(3)) if center else 0.0,
                "size_x": float(sz.group(1)) if sz else 1.0,
                "size_y": float(sz.group(2)) if sz else 1.0,
                "size_z": float(sz.group(3)) if sz else 1.0,
                "material_guid": _parse_material_guid(block),
            }
        if kind == "SphereCollider":
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            trig = re.search(r"(?m)^\s+m_IsTrigger:\s*(\d+)", block)
            center = re.search(
                r"m_Center:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
                r"\s*z:\s*([^}]+)\}", block)
            rad = re.search(r"(?m)^\s+m_Radius:\s*([0-9.eE+-]+)", block)
            rec["collider3d"] = {
                "kind": "sphere",
                "enabled": int(en.group(1)) if en else 1,
                "is_trigger": int(trig.group(1)) if trig else 0,
                "offset_x": float(center.group(1)) if center else 0.0,
                "offset_y": float(center.group(2)) if center else 0.0,
                "offset_z": float(center.group(3)) if center else 0.0,
                "radius": float(rad.group(1)) if rad else 0.5,
                "material_guid": _parse_material_guid(block),
            }
        if kind == "Animation":
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            play = re.search(r"(?m)^\s+m_PlayAutomatically:\s*(\d+)", block)
            wrap = re.search(r"(?m)^\s+m_WrapMode:\s*(\d+)", block)
            rec["animation"] = {
                "enabled": int(en.group(1)) if en else 1,
                "play_automatically": int(play.group(1)) if play else 1,
                "wrap_mode": int(wrap.group(1)) if wrap else 0,
                "clip_guid": _parse_asset_guid_ref(block, "m_Animation"),
            }
        if kind == "Animator":
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            rec["animator"] = {
                "enabled": int(en.group(1)) if en else 1,
                "controller_guid": _parse_asset_guid_ref(block, "m_Controller"),
            }
        by_id[file_id] = rec

    # PrefabInstance.m_TransformParent applies to stripped Transforms that
    # reference the instance (Unity does not repeat m_Father on stripped).
    for rec in by_id.values():
        if rec.get("kind") != "Transform" or rec.get("father_id"):
            continue
        pm = re.search(
            r"(?m)^\s+m_PrefabInstance:\s*\{fileID:\s*(\d+)\}",
            rec.get("raw") or "")
        if not pm:
            continue
        pref = by_id.get(pm.group(1))
        if not pref or pref.get("kind") != "PrefabInstance":
            continue
        if pref.get("father_id"):
            rec["father_id"] = pref["father_id"]
        for key in ("pos", "rot", "scale"):
            if key not in rec and key in pref:
                rec[key] = pref[key]

    world_cache = {}

    # Join MonoBehaviour + Transform + SpriteRenderer onto the GameObject.
    gos = [r for r in by_id.values() if r.get("kind") == "GameObject"]
    for go in gos:
        kids = []
        for mid in re.findall(r"fileID:\s*(\d+)", go["raw"]):
            if mid in by_id and by_id[mid] is not go:
                kids.append(by_id[mid])
        pos = (0.0, 0.0, 0.0)
        scale = (1.0, 1.0, 1.0)
        rot = (0.0, 0.0, 0.0, 1.0)
        local_pos = pos
        local_rot = rot
        local_scale = scale
        xf = None
        script = None
        fields = {}
        sprite = None
        cam = None
        rb2d = None
        rb3d = None
        col2d = None
        col3d = None
        anim = None
        animator = None
        for k in kids:
            if k.get("kind") == "Transform":
                xf = k
            if k.get("pos"):
                pos = k["pos"]
            if k.get("scale"):
                scale = k["scale"]
            if k.get("rot"):
                rot = k["rot"]
            if k.get("kind") == "MonoBehaviour":
                fields.update(k.get("fields") or {})
                g = k.get("guid")
                if g and g in guid_to_script:
                    script = guid_to_script[g]
            if k.get("kind") == "SpriteRenderer" and k.get("sprite"):
                sprite = dict(k["sprite"])
            if k.get("kind") == "Camera" and k.get("camera"):
                cam = dict(k["camera"])
            if k.get("kind") == "Rigidbody2D" and k.get("rigidbody2d"):
                rb2d = dict(k["rigidbody2d"])
            if k.get("kind") == "Rigidbody" and k.get("rigidbody"):
                rb3d = dict(k["rigidbody"])
            if k.get("kind") in ("BoxCollider2D", "CircleCollider2D") and k.get(
                    "collider2d"):
                col2d = dict(k["collider2d"])
            if k.get("kind") in ("BoxCollider", "SphereCollider") and k.get(
                    "collider3d"):
                col3d = dict(k["collider3d"])
            if k.get("kind") == "Animation" and k.get("animation"):
                anim = dict(k["animation"])
            if k.get("kind") == "Animator" and k.get("animator"):
                animator = dict(k["animator"])
        local_pos, local_rot, local_scale = pos, rot, scale
        father_id = xf.get("father_id") if xf else None
        if xf is not None:
            pos, rot, scale = _resolve_world_trs(
                xf["file_id"], by_id, world_cache)
        # Resolve Animation / Animator → clip guid (Animator via controller default).
        player = None
        if anim and anim.get("enabled", 1) and anim.get("clip_guid"):
            cg = anim["clip_guid"]
            if cg in anim_clips:
                player = {
                    "kind": "animation",
                    "clip_guid": cg,
                    "playing": int(anim.get("play_automatically") or 0),
                    "speed": 1.0,
                    "loop": int(anim_clips[cg].get("loop") or 0),
                }
        elif animator and animator.get("enabled", 1) and animator.get(
                "controller_guid"):
            cg = anim_controllers.get(animator["controller_guid"])
            if cg and cg in anim_clips:
                player = {
                    "kind": "animator",
                    "clip_guid": cg,
                    "playing": 1,  # Animator plays default state
                    "speed": 1.0,
                    "loop": int(anim_clips[cg].get("loop") or 0),
                }
        rb_mat2 = (rb2d or {}).get("material_guid")
        rb_mat3 = (rb3d or {}).get("material_guid")
        if col2d and col2d.get("enabled", 1):
            sx = abs(float(scale[0]))
            sy = abs(float(scale[1]))
            rz = _quat_z_rad(rot[0], rot[1], rot[2], rot[3])
            col2d["ox"] = float(col2d.get("offset_x", 0.0)) * sx
            col2d["oy"] = float(col2d.get("offset_y", 0.0)) * sy
            col2d["cos_z"] = math.cos(rz)
            col2d["sin_z"] = math.sin(rz)
            if col2d.get("kind") == "box":
                col2d["hw"] = abs(float(col2d.get("size_x", 1.0))) * sx * 0.5
                col2d["hh"] = abs(float(col2d.get("size_y", 1.0))) * sy * 0.5
            else:
                mxy = sx if sx > sy else sy
                col2d["hw"] = float(col2d.get("radius", 0.5)) * mxy
                col2d["hh"] = col2d["hw"]
            mat = _resolve_mat2d(col2d.get("material_guid"), rb_mat2, mats2d)
            col2d["friction"] = float(mat["friction"])
            col2d["bounciness"] = float(mat["bounciness"])
            col2d["friction_combine"] = int(mat["friction_combine"])
            col2d["bounce_combine"] = int(mat["bounce_combine"])
        else:
            col2d = None
        if col3d and col3d.get("enabled", 1):
            sx = abs(float(scale[0]))
            sy = abs(float(scale[1]))
            sz = abs(float(scale[2]))
            col3d["ox"] = float(col3d.get("offset_x", 0.0)) * sx
            col3d["oy"] = float(col3d.get("offset_y", 0.0)) * sy
            col3d["oz"] = float(col3d.get("offset_z", 0.0)) * sz
            if col3d.get("kind") == "box":
                col3d["hw"] = abs(float(col3d.get("size_x", 1.0))) * sx * 0.5
                col3d["hh"] = abs(float(col3d.get("size_y", 1.0))) * sy * 0.5
                col3d["hd"] = abs(float(col3d.get("size_z", 1.0))) * sz * 0.5
            else:
                mxyz = max(sx, sy, sz)
                col3d["hw"] = float(col3d.get("radius", 0.5)) * mxyz
                col3d["hh"] = col3d["hw"]
                col3d["hd"] = col3d["hw"]
            mat = _resolve_mat3d(col3d.get("material_guid"), rb_mat3, mats3d)
            col3d["dynamic_friction"] = float(mat["dynamic_friction"])
            col3d["static_friction"] = float(mat["static_friction"])
            col3d["bounciness"] = float(mat["bounciness"])
            col3d["friction_combine"] = int(mat["friction_combine"])
            col3d["bounce_combine"] = int(mat["bounce_combine"])
        else:
            col3d = None
        if sprite and sprite.get("enabled", 1) and sprite.get("has_sprite"):
            # Extent filled after PNG load via pixels / pixelsPerUnit * scale.
            sprite["scale_x"] = abs(float(scale[0]))
            sprite["scale_y"] = abs(float(scale[1]))
            rz = _quat_z_rad(rot[0], rot[1], rot[2], rot[3])
            sprite["rot_z"] = rz
            sprite["cos_z"] = math.cos(rz)
            sprite["sin_z"] = math.sin(rz)
        else:
            sprite = None
        class_name = None
        if script:
            class_name = _class_name_from_cs(script)
        if cam is not None:
            cameras.append({
                "name": go.get("name") or "Camera",
                "pos": pos,
                "rot": rot,
                "main": (go.get("tag") == "MainCamera"
                         or (go.get("name") or "").lower() == "main camera"),
                "orthographic": cam["orthographic"],
                "orthographic_size": cam["orthographic_size"],
                "bg_r": cam["bg_r"],
                "bg_g": cam["bg_g"],
                "bg_b": cam["bg_b"],
                "near_clip": cam["near_clip"],
                "far_clip": cam["far_clip"],
            })
        # Camera-only GOs are not packed as scripted instances.
        if (cam is not None and script is None and sprite is None
                and not rb2d and not rb3d and not col2d and not col3d
                and not player):
            continue
        has_mb = any(k.get("kind") == "MonoBehaviour" for k in kids)
        # Transform-only parents (e.g. Rig) are hierarchy nodes, not instances.
        if (script is None and sprite is None and not rb2d and not rb3d
                and cam is None and not has_mb and not col2d and not col3d
                and not player):
            continue
        objects.append({
            "name": go.get("name") or "obj",
            "pos": pos,
            "rot": rot,
            "local_pos": local_pos,
            "local_rot": local_rot,
            "local_scale": local_scale,
            "father_id": father_id,
            "fields": fields,
            "script": script,
            "class": class_name or go.get("name") or "Obj",
            "sprite": sprite,
            "rigidbody2d": rb2d,
            "rigidbody": rb3d,
            "collider2d": col2d,
            "collider3d": col3d,
            "anim_player": player,
        })
    # Stash clip assets on a sentinel for pack() — returned via lights? No.
    # Attach to a module-level isn't clean. Return clips via objects meta:
    # pack() reloads clips. Store on each player the clip snapshot.
    for o in objects:
        p = o.get("anim_player")
        if not p:
            continue
        clip = anim_clips.get(p["clip_guid"])
        if clip:
            p["clip"] = clip
    return objects, lights, cameras


def parse_godot_tscn(text):
    """Godot .tscn nodes with a script class name and exported numbers."""
    objects = []
    chunks = re.split(r"(?m)^\[node ", text)
    for chunk in chunks[1:]:
        hm = re.match(r'name="([^"]+)"', chunk)
        if not hm:
            continue
        name = hm.group(1)
        pos = (0.0, 0.0, 0.0)
        pm = re.search(
            r"position\s*=\s*Vector[23]\(\s*([^,\)]+),\s*([^,\)]+)"
            r"(?:,\s*([^)]+))?\)", chunk)
        if pm:
            pos = (float(pm.group(1)), float(pm.group(2)),
                   float(pm.group(3) or 0.0))
        fields = {}
        class_name = name
        sm = re.search(r"(?m)^script_class\s*=\s*\"([^\"]+)\"", chunk)
        if sm:
            class_name = sm.group(1)
        for fm in re.finditer(r"(?m)^(\w+)\s*=\s*(-?\d+(?:\.\d+)?)\s*$",
                              chunk):
            key = fm.group(1)
            if key in ("position",):
                continue
            val = fm.group(2)
            fields[key] = float(val) if "." in val else int(val)
        objects.append({
            "name": name, "pos": pos, "fields": fields,
            "script": None, "class": class_name,
            "sprite": None,
        })
    return objects


def parse_blender_json(text):
    """Minimal Blender dump: {\"objects\": [{\"name\",\"class\",\"pos\",\"fields\"}]}."""
    import json
    data = json.loads(text)
    out = []
    for o in data.get("objects") or []:
        pos = o.get("pos") or [0, 0, 0]
        out.append({
            "name": o.get("name") or "obj",
            "pos": (float(pos[0]), float(pos[1]),
                    float(pos[2] if len(pos) > 2 else 0)),
            "fields": o.get("fields") or {},
            "script": None,
            "class": o.get("class") or o.get("name") or "Obj",
            "sprite": o.get("sprite"),
        })
    return out


def _class_name_from_cs(path):
    try:
        text = _read(path)
    except IOError:
        return None
    scan = cs2cpp._blank(text)
    for kind, name, _s, _b, _c in cs2cpp._find_types(scan):
        if kind in ("class", "struct"):
            return name
    return None


def _string_literal_value(expr):
    """Return the string inside a C# literal, or None if not a plain literal."""
    s = (expr or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return None


def _ast_find_getcomponent_chains(text):
    """Locate `GameObject.Find(...).GetComponent<T>()` chains via cpprust AST.

    Uses `_match_paren` / `_match_angle` (same helpers csrust → cpprust uses)
    so nested calls and generics are not split by naive regex. Yields dicts
    with source spans, Find args, component type, and optional `.field`.
    """
    import tools.cpprust as cpprust
    scan = cs2cpp._blank(text)
    out = []
    for m in re.finditer(
            r"(?:UnityEngine\.)?GameObject\.Find\s*\(", scan):
        open_p = m.end() - 1
        close_p = cpprust._match_paren(scan, open_p)
        if close_p is None:
            continue
        find_args = text[open_p + 1:close_p]
        end = close_p + 1
        comp_ty = None
        field = None
        # .GetComponent < T > ( ... )
        gm = re.match(r"\s*\.\s*GetComponent\s*<", scan[end:])
        if gm:
            angle_open = end + gm.end() - 1
            angle_close = cpprust._match_angle(scan, angle_open)
            if angle_close is None:
                continue
            comp_ty = text[angle_open + 1:angle_close].strip()
            if "." in comp_ty:
                comp_ty = comp_ty.rsplit(".", 1)[-1]
            after_angle = scan[angle_close + 1:]
            pm = re.match(r"\s*\(", after_angle)
            if not pm:
                continue
            g_open = angle_close + 1 + pm.start()
            # pm matches optional space then (; open paren index:
            g_open = angle_close + 1 + after_angle.find("(")
            g_close = cpprust._match_paren(scan, g_open)
            if g_close is None:
                continue
            end = g_close + 1
            fm = re.match(
                r"\s*\.\s*([A-Za-z_]\w*)\b(?:\s*\.\s*([xyz]))?",
                scan[end:])
            if fm:
                field = fm.group(1)
                axis = fm.group(2)
                end = end + fm.end()
            else:
                axis = None
        else:
            axis = None
        out.append({
            "start": m.start(),
            "end": end,
            "find_args": find_args.strip(),
            "component": comp_ty,
            "field": field,
            "axis": axis,
        })
    # Standalone this.GetComponent<T>() / GetComponent<T>()
    for m in re.finditer(
            r"(?:(?<![\w.])this\s*\.\s*)?GetComponent\s*<", scan):
        # Skip if already covered as part of a Find chain.
        if any(c["start"] <= m.start() < c["end"] for c in out):
            continue
        angle_open = m.end() - 1
        angle_close = cpprust._match_angle(scan, angle_open)
        if angle_close is None:
            continue
        comp_ty = text[angle_open + 1:angle_close].strip()
        if "." in comp_ty:
            comp_ty = comp_ty.rsplit(".", 1)[-1]
        after_angle = scan[angle_close + 1:]
        if after_angle.find("(") < 0:
            continue
        g_open = angle_close + 1 + after_angle.find("(")
        g_close = cpprust._match_paren(scan, g_open)
        if g_close is None:
            continue
        end = g_close + 1
        field = None
        axis = None
        fm = re.match(
            r"\s*\.\s*([A-Za-z_]\w*)\b(?:\s*\.\s*([xyz]))?",
            scan[end:])
        if fm:
            field = fm.group(1)
            axis = fm.group(2)
            end = end + fm.end()
        out.append({
            "start": m.start(),
            "end": end,
            "find_args": None,  # this GameObject
            "component": comp_ty,
            "field": field,
            "axis": axis,
            "on_this": True,
        })
    out.sort(key=lambda c: c["start"], reverse=True)
    return out


def _build_go_tables(plan):
    """Authored GameObject name → {MonoBehaviour class: instance index}."""
    names = []
    seen = set()
    comps = {}  # name -> {class: idx}
    for cname, cl in sorted(plan["classes"].items()):
        for i, o in enumerate(cl.get("instances") or []):
            n = o.get("name") or "obj"
            if n not in seen:
                seen.add(n)
                names.append(n)
            comps.setdefault(n, {})[cname] = i
    return names, comps


def _collect_addcomponent_types(analyses):
    types = set()
    for a in analyses:
        types |= set(a.get("addcomponent_types") or [])
    return types


def _addcomponent_budget(analyses, plan):
    """Extra slots per type: one per instance of each class that calls AddComponent<T>.

    A successful first add needs a pool slot. DisallowMultiple types that
    already have an authored component never allocate; budget still covers
    GOs that lack one.
    """
    budget = {}
    class_n = {n: int(cl.get("n") or 0) for n, cl in plan["classes"].items()}
    for a in analyses:
        for c in a.get("classes") or []:
            cname = c["name"]
            n = class_n.get(cname, 1) or 1
            bodies = "\n".join(m.get("body") or "" for m in c.get("methods") or [])
            for m in re.finditer(
                    r"AddComponent\s*<\s*(?:UnityEngine\.)?(\w+)\s*>", bodies):
                t = m.group(1)
                budget[t] = budget.get(t, 0) + n
    return budget


def _disallow_multiple_types(analyses):
    """Type names that refuse a second AddComponent (Unity attribute / builtins)."""
    out = set(_DISALLOW_MULTIPLE_BUILTINS)
    for a in analyses:
        for c in a.get("classes") or []:
            if c.get("disallow_multiple"):
                out.add(c["name"])
    return out


def _gos_with_sprite(plan):
    """Authored GameObject names that already have a SpriteRenderer."""
    names = set()
    for cl in (plan.get("classes") or {}).values():
        for o in cl.get("instances") or []:
            if o.get("sprite"):
                names.add(o.get("name") or "")
    return names


def _validate_addcomponent_types(types, plan):
    known = set(plan.get("classes") or {}) | _ADDABLE_BUILTINS
    for t in sorted(types):
        if t in _REFUSED_ADDCOMPONENT:
            raise PackError(
                "AddComponent<%s>: unity_pack does not invent %s assets / "
                "systems. Keep that component in the authored Unity project."
                % (t, t))
        if t not in known:
            raise PackError(
                "AddComponent<%s>: no packed %s — add an authored scene "
                "instance of that MonoBehaviour, or use a supported builtin "
                "(%s)."
                % (t, t, ", ".join(sorted(_ADDABLE_BUILTINS))))


def _rewrite_addcomponent(text, plan, this_class):
    """Lower gameObject.AddComponent<T>() / AddComponent<T>() to C helpers.

    Returns (text, locals_ty) where locals_ty maps local name → component type
    for Console/Debug ToString wrapping.
    """
    this_idn = _c_ident(this_class)
    go_expr = "_engine_go_of_%s(i)" % this_idn
    locals_ty = {}

    def repl_typed(m):
        var, comp = m.group(1), m.group(2)
        locals_ty[var] = comp
        return "int %s = GameObject_AddComponent_%s(%s)" % (
            var, _c_ident(comp), go_expr)

    # Camera cam = gameObject.AddComponent<Camera>();
    text = re.sub(
        r"(?:(?:UnityEngine\.)?\w+)\s+(\w+)\s*=\s*"
        r"(?:(?:this|gameObject)\s*\.\s*)?AddComponent\s*<\s*"
        r"(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)",
        repl_typed, text)

    def repl_bare(m):
        return "GameObject_AddComponent_%s(%s)" % (
            _c_ident(m.group(1)), go_expr)

    text = re.sub(
        r"(?:(?:this|gameObject)\s*\.\s*)?AddComponent\s*<\s*"
        r"(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)",
        repl_bare, text)
    return text, locals_ty


def _wrap_log_component_tostring(text, locals_ty):
    """Console/Debug of an AddComponent local → Type_ToString(index)."""
    if not locals_ty:
        return text
    out = []
    i = 0
    while True:
        m = re.search(r"(?:Console_WriteLine|Debug_Log)\s*\(", text[i:])
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:i + m.start()])
        call = m.group(0)
        callee = re.match(r"(Console_WriteLine|Debug_Log)", call).group(1)
        start = i + m.end()
        depth = 1
        j = start
        while j < len(text) and depth:
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            out.append(text[i + m.start():])
            break
        args = text[start:j].strip()
        if re.match(r"^[A-Za-z_]\w*$", args) and args in locals_ty:
            ty = locals_ty[args]
            args = "%s_ToString(%s)" % (_c_ident(ty), args)
        out.append("%s(%s)" % (callee, args))
        i = j + 1
    return "".join(out)


_PHYSICS_COMPONENTS = frozenset(("Rigidbody2D", "Rigidbody"))


def _build_rigidbody_tables(plan):
    """Authored Rigidbody2D / Rigidbody → packed tables linked to MB instances."""
    rb2d = []
    rb3d = []
    go_rb2d = {}  # go_name -> rb2d index
    go_rb3d = {}
    for cname, cl in sorted(plan["classes"].items()):
        for i, o in enumerate(cl.get("instances") or []):
            n = o.get("name") or "obj"
            r2 = o.get("rigidbody2d")
            if r2:
                go_rb2d[n] = len(rb2d)
                rb2d.append({
                    "name": n,
                    "owner_class": cname,
                    "owner_inst": i,
                    "body_type": int(r2.get("body_type") or 0),
                    "mass": float(r2.get("mass") or 1.0),
                    "gravity_scale": float(r2.get("gravity_scale") or 1.0),
                    "vel_x": float(r2.get("vel_x") or 0.0),
                    "vel_y": float(r2.get("vel_y") or 0.0),
                })
            r3 = o.get("rigidbody")
            if r3:
                go_rb3d[n] = len(rb3d)
                rb3d.append({
                    "name": n,
                    "owner_class": cname,
                    "owner_inst": i,
                    "mass": float(r3.get("mass") or 1.0),
                    "use_gravity": int(r3.get("use_gravity")
                                       if r3.get("use_gravity") is not None
                                       else 1),
                    "vel_x": float(r3.get("vel_x") or 0.0),
                    "vel_y": float(r3.get("vel_y") or 0.0),
                    "vel_z": float(r3.get("vel_z") or 0.0),
                })
    return rb2d, rb3d, go_rb2d, go_rb3d


def _build_collider2d_tables(plan):
    """Authored BoxCollider2D / CircleCollider2D → packed contact table."""
    cols = []
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    # Map (class, inst) → rb2d index for dynamic flag.
    rb_of = {}
    for ri, r in enumerate(plan.get("rigidbody2d") or []):
        rb_of[(r["owner_class"], r["owner_inst"])] = ri
    for cname, cl in sorted(plan["classes"].items()):
        cid = class_ids[cname]
        for i, o in enumerate(cl.get("instances") or []):
            c = o.get("collider2d")
            if not c or not c.get("enabled", 1):
                continue
            rb_i = rb_of.get((cname, i))
            body = 2  # static (no RB)
            if rb_i is not None:
                body = int((plan["rigidbody2d"][rb_i]).get("body_type") or 0)
            kind = 0 if c.get("kind") == "box" else 1
            cols.append({
                "name": o.get("name") or "obj",
                "owner_class": cname,
                "owner_class_id": cid,
                "owner_inst": i,
                "rb2d": rb_i if rb_i is not None else -1,
                "body_type": body,  # 0 dynamic, 1 kinematic, 2 static
                "kind": kind,
                "is_trigger": int(c.get("is_trigger") or 0),
                "ox": float(c.get("ox") or 0.0),
                "oy": float(c.get("oy") or 0.0),
                "hw": float(c.get("hw") or 0.5),
                "hh": float(c.get("hh") or 0.5),
                "cos_z": float(c.get("cos_z") or 1.0),
                "sin_z": float(c.get("sin_z") or 0.0),
                "friction": float(c.get("friction")
                                  if c.get("friction") is not None
                                  else _DEFAULT_MAT2D["friction"]),
                "bounciness": float(c.get("bounciness")
                                    if c.get("bounciness") is not None
                                    else 0.0),
                "friction_combine": int(c.get("friction_combine") or 0),
                "bounce_combine": int(c.get("bounce_combine") or 0),
            })
    return cols


def _build_collider3d_tables(plan):
    """Authored BoxCollider / SphereCollider → packed contact table."""
    cols = []
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    rb_of = {}
    for ri, r in enumerate(plan.get("rigidbody") or []):
        rb_of[(r["owner_class"], r["owner_inst"])] = ri
    for cname, cl in sorted(plan["classes"].items()):
        cid = class_ids[cname]
        for i, o in enumerate(cl.get("instances") or []):
            c = o.get("collider3d")
            if not c or not c.get("enabled", 1):
                continue
            rb_i = rb_of.get((cname, i))
            body = 0 if rb_i is not None else 2  # dynamic if RB else static
            kind = 0 if c.get("kind") == "box" else 1
            cols.append({
                "name": o.get("name") or "obj",
                "owner_class": cname,
                "owner_class_id": cid,
                "owner_inst": i,
                "rb3d": rb_i if rb_i is not None else -1,
                "body_type": body,
                "kind": kind,
                "is_trigger": int(c.get("is_trigger") or 0),
                "ox": float(c.get("ox") or 0.0),
                "oy": float(c.get("oy") or 0.0),
                "oz": float(c.get("oz") or 0.0),
                "hw": float(c.get("hw") or 0.5),
                "hh": float(c.get("hh") or 0.5),
                "hd": float(c.get("hd") or 0.5),
                "dynamic_friction": float(
                    c.get("dynamic_friction")
                    if c.get("dynamic_friction") is not None
                    else _DEFAULT_MAT3D["dynamic_friction"]),
                "static_friction": float(
                    c.get("static_friction")
                    if c.get("static_friction") is not None
                    else _DEFAULT_MAT3D["static_friction"]),
                "bounciness": float(c.get("bounciness")
                                    if c.get("bounciness") is not None
                                    else 0.0),
                "friction_combine": int(c.get("friction_combine") or 0),
                "bounce_combine": int(c.get("bounce_combine") or 0),
            })
    return cols


def _build_animation_tables(plan):
    """Authored Animation / Animator players + shared clip keyframe tables.

    Root position curves apply as deltas from the clip's t=0 sample onto the
    authored rest pose so one clip can drive multiple GOs.
    """
    clips_by_guid = {}
    clip_list = []
    players = []
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}

    def _ensure_clip(guid, clip):
        if guid in clips_by_guid:
            return clips_by_guid[guid]
        pos = list(clip.get("pos_keys") or [])
        if pos:
            t0x, t0y, t0z = pos[0][1], pos[0][2], pos[0][3]
            pos = [(t, x - t0x, y - t0y, z - t0z) for t, x, y, z in pos]
        else:
            pos = [(0.0, 0.0, 0.0, 0.0)]
        idx = len(clip_list)
        entry = {
            "guid": guid,
            "name": clip.get("name") or "Clip",
            "length": float(clip.get("length") or 1.0),
            "loop": int(clip.get("loop") or 0),
            "pos_keys": pos,
            "key_begin": 0,
            "key_count": len(pos),
        }
        clips_by_guid[guid] = idx
        clip_list.append(entry)
        return idx

    for cname, cl in sorted(plan["classes"].items()):
        cid = class_ids[cname]
        for i, o in enumerate(cl.get("instances") or []):
            p = o.get("anim_player")
            if not p or not p.get("clip"):
                continue
            guid = p["clip_guid"]
            ci = _ensure_clip(guid, p["clip"])
            rest = o.get("pos") or (0.0, 0.0, 0.0)
            players.append({
                "name": o.get("name") or "obj",
                "owner_class": cname,
                "owner_class_id": cid,
                "owner_inst": i,
                "clip": ci,
                "playing": int(p.get("playing") or 0),
                "speed": float(p.get("speed") or 1.0),
                "loop": int(p.get("loop")
                            if p.get("loop") is not None
                            else clip_list[ci]["loop"]),
                "rest_x": float(rest[0]),
                "rest_y": float(rest[1]),
                "rest_z": float(rest[2]) if len(rest) > 2 else 0.0,
                "kind": 0 if p.get("kind") == "animation" else 1,
            })

    keys = []
    for c in clip_list:
        c["key_begin"] = len(keys)
        for t, x, y, z in c["pos_keys"]:
            keys.append({"t": t, "x": x, "y": y, "z": z})
        c["key_count"] = len(c["pos_keys"])
    return {"clips": clip_list, "keys": keys, "players": players}


def _rewrite_rigidbody_assigns(text, plan, this_class):
    """Lower GetComponent<Rigidbody*>().velocity = new VectorN(...);"""
    this_idn = _c_ident(this_class)
    go_this = "_engine_go_of_%s(i)" % this_idn

    def repl_2d(m):
        args = _split_call_args(m.group(1))
        if len(args) < 2:
            return m.group(0)
        return (
            "{ int _up_rb = GameObject_GetComponent_Rigidbody2D(%s); "
            "if (_up_rb >= 0) { _Rigidbody2D_vel_x[_up_rb] = (%s); "
            "_Rigidbody2D_vel_y[_up_rb] = (%s); } }"
            % (go_this, args[0], args[1])
        )

    def repl_3d(m):
        args = _split_call_args(m.group(1))
        if len(args) < 3:
            return m.group(0)
        return (
            "{ int _up_rb = GameObject_GetComponent_Rigidbody(%s); "
            "if (_up_rb >= 0) { _Rigidbody_vel_x[_up_rb] = (%s); "
            "_Rigidbody_vel_y[_up_rb] = (%s); "
            "_Rigidbody_vel_z[_up_rb] = (%s); } }"
            % (go_this, args[0], args[1], args[2])
        )

    text = re.sub(
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody2D\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
        r"new\s+Vector2\s*\((.*?)\)\s*;",
        repl_2d, text, flags=re.S)
    text = re.sub(
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
        r"new\s+Vector3\s*\((.*?)\)\s*;",
        repl_3d, text, flags=re.S)
    return text


def _rewrite_find_getcomponent(text, plan, this_class):
    """Lower Find/GetComponent chains using the authored GO tables.

    `GameObject.Find` name lookup is always runtime (`strcmp` on the packed
    name table) — missing names yield -1 like Unity null, not a PackError.
    Authored Rigidbody / Rigidbody2D are first-class GetComponent targets.
    """
    chains = _ast_find_getcomponent_chains(text)
    if not chains:
        return text

    def _zero_for_field(comp, field):
        cl = (plan.get("classes") or {}).get(comp) or {}
        for name, ty, _bits, kind in cl.get("members") or []:
            if name != field:
                continue
            if kind in ("f16", "f32") or ty == "float":
                return "0.f"
            return "0"
        return "0.f"

    def _rb_field_expr(comp, field, axis, go_expr):
        """GetComponent<Rigidbody2D/Rigidbody>().velocity.x / gravityScale."""
        get = "GameObject_GetComponent_%s(%s)" % (_c_ident(comp), go_expr)
        fl = (field or "").lower()
        if comp == "Rigidbody2D":
            if fl in ("gravityscale",):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 ? 0.f : _Rigidbody2D_gravity_scale[_up_rb]; })"
                        % get)
            if fl in ("velocity", "linearvelocity") and axis in ("x", "y"):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 ? 0.f : _Rigidbody2D_vel_%s[_up_rb]; })"
                        % (get, axis))
            if fl in ("mass",):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 ? 0.f : _Rigidbody2D_mass[_up_rb]; })"
                        % get)
        if comp == "Rigidbody":
            if fl in ("velocity", "linearvelocity") and axis in ("x", "y", "z"):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 ? 0.f : _Rigidbody_vel_%s[_up_rb]; })"
                        % (get, axis))
            if fl in ("mass",):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 ? 0.f : _Rigidbody_mass[_up_rb]; })"
                        % get)
            if fl in ("usegravity",):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 ? 0 : _Rigidbody_use_gravity[_up_rb]; })"
                        % get)
        raise PackError(
            "GetComponent<%s>.%s: unsupported Rigidbody field "
            "(use velocity.x/y, gravityScale, mass)"
            % (comp, field or "?"))

    def _field_after_get(comp, field, go_expr, axis=None):
        if comp in _PHYSICS_COMPONENTS:
            return _rb_field_expr(comp, field, axis, go_expr)
        idn = _c_ident(comp)
        zero = _zero_for_field(comp, field)
        return (
            "({ int _up_gc = GameObject_GetComponent_%s(%s); "
            "_up_gc < 0 ? %s : %s_get_%s((unsigned)_up_gc); })"
            % (idn, go_expr, zero, idn, field)
        )

    def _known_component(comp):
        return (comp in (plan.get("classes") or {})
                or comp in _PHYSICS_COMPONENTS
                or comp in _ADDABLE_BUILTINS
                or comp in set(plan.get("addcomponent_types") or []))

    for ch in chains:
        comp = ch.get("component")
        field = ch.get("field")
        axis = ch.get("axis")
        if ch.get("on_this"):
            if not comp:
                raise PackError("GetComponent requires a type argument")
            this_idn = _c_ident(this_class)
            if not _known_component(comp):
                raise PackError(
                    "GetComponent<%s>: no authored %s in the scene — "
                    "unity_pack does not invent components" % (comp, comp))
            go_expr = "_engine_go_of_%s(i)" % this_idn
            if field:
                repl = _field_after_get(comp, field, go_expr, axis)
            else:
                repl = "GameObject_GetComponent_%s(%s)" % (
                    _c_ident(comp), go_expr)
            text = text[:ch["start"]] + repl + text[ch["end"]:]
            continue

        find_args = ch.get("find_args") or ""
        go_expr = "GameObject_Find(%s)" % find_args
        if not comp:
            repl = go_expr
        else:
            if not _known_component(comp):
                raise PackError(
                    "GetComponent<%s>: no authored %s in the scene — "
                    "unity_pack does not invent components" % (comp, comp))
            if field:
                repl = _field_after_get(comp, field, go_expr, axis)
            else:
                repl = "GameObject_GetComponent_%s(%s)" % (
                    _c_ident(comp), go_expr)
        text = text[:ch["start"]] + repl + text[ch["end"]:]
    return text


def analyze_script(path, text=None):
    """Fields, methods, Unity API used, whether the script spawns."""
    if text is None:
        text = _read(path)
    _check_csharp_lex(path, text)
    scan = cs2cpp._blank(text)
    apis = set()
    addcomponent_types = set()
    for m in _UNITY_API.finditer(scan):
        token = m.group(0)
        if token.startswith("AddComponent"):
            tm = re.search(r"AddComponent\s*<\s*(?:UnityEngine\.)?(\w+)\s*>",
                           token)
            if tm:
                tname = tm.group(1)
                addcomponent_types.add(tname)
                apis.add("AddComponent<%s>" % tname)
        elif token.startswith("GetComponent"):
            apis.add("GetComponent")
        elif "GameObject.Find" in token or token == "GameObject.Find":
            apis.add("GameObject.Find")
        else:
            apis.add(token)
    # Catch AddComponent even if _UNITY_API missed a variant.
    for m in re.finditer(
            r"AddComponent\s*<\s*(?:UnityEngine\.)?(\w+)\s*>", scan):
        addcomponent_types.add(m.group(1))
        apis.add("AddComponent<%s>" % m.group(1))
    # AST pass: precise Find / GetComponent detection (cpprust paren/angle).
    getcomponent_types = set()
    for ch in _ast_find_getcomponent_chains(text):
        if ch.get("find_args") is not None and not ch.get("on_this"):
            apis.add("GameObject.Find")
        if ch.get("component"):
            apis.add("GetComponent")
            getcomponent_types.add(ch["component"])
            if ch["component"] in _PHYSICS_COMPONENTS:
                apis.add(ch["component"])
        elif ch.get("on_this"):
            apis.add("GetComponent")
    if "transform.position" in scan:
        apis.add("transform.position")
    if re.search(r"using\s+UnityEngine\.UI\b", scan):
        apis.add("UnityEngine.UI")
    if re.search(r"\bInputAction\b", scan):
        apis.add("InputAction")
    # Keyboard lives in UnityEngine.InputSystem — only when in scope.
    has_input_system = bool(
        re.search(r"using\s+UnityEngine\.InputSystem\b", scan)
        or re.search(r"UnityEngine\.InputSystem\.Keyboard\b", scan)
    )
    apis.discard("Keyboard.current")  # may have matched via _UNITY_API
    keyboard_keys = set()
    if has_input_system:
        for m in _KEYBOARD_KEY.finditer(scan):
            apis.add("Keyboard.current")
            keyboard_keys.add(m.group(1))
        if re.search(
                r"(?:UnityEngine\.InputSystem\.)?Keyboard\.current\b", scan):
            apis.add("Keyboard.current")
    elif (re.search(r"(?<![\w.])Keyboard\.current\b", scan)
          or _KEYBOARD_KEY.search(scan)):
        apis.add("Keyboard")
    if re.search(r"(?:UnityEngine\.)?Debug\.Log\s*\(", scan):
        apis.add("Debug.Log")
    if re.search(r"(?<![\w.])print\s*\(", scan):
        apis.add("print")
    # C# string + value must not become C pointer arithmetic.
    if re.search(r'"\s*\+', scan):
        apis.add("string.+")
    has_system = bool(re.search(r"using\s+System\b", scan))
    apis.discard("Console.WriteLine")  # may have matched via _UNITY_API
    if re.search(r"System\.Console\.WriteLine\s*\(", scan):
        apis.add("Console.WriteLine")
    elif has_system and re.search(r"(?<![\w.])Console\.WriteLine\s*\(", scan):
        apis.add("Console.WriteLine")
    elif re.search(r"(?<![\w.])Console\.WriteLine\s*\(", scan):
        # Bare Console without using System — not in scope.
        apis.add("Console")

    spawns = bool(_SPAWN.search(scan))
    uses_z = bool(re.search(r"(?<![\w.])Vector3\b", scan)
                  or re.search(r"(?<![\w.])Quaternion\b", scan)
                  or re.search(r"transform\.position\.z", scan))
    writes_pos = bool(re.search(
        r"transform\.position\s*=|"
        r"transform\.position\s*\+=|"
        r"transform\.Translate", scan))

    types = cs2cpp._find_types(scan)
    classes = []
    for kind, name, start, brace, close in types:
        if kind not in ("class", "struct"):
            continue
        body = text[brace + 1:close]
        bscan = scan[brace + 1:close]
        fields = _fields_in(body, bscan)
        methods = _methods_in(body, bscan)
        refs = []
        for f in fields:
            if f["ty"] not in _PRIM and f["ty"] not in (
                    "Vector2", "Vector3", "Quaternion", "string"):
                refs.append(f)
        # Attributes immediately before the type declaration.
        pre = scan[max(0, start - 200):start]
        disallow_multiple = bool(re.search(
            r"\[DisallowMultipleComponent\]", pre))
        classes.append({
            "name": name, "kind": kind, "fields": fields,
            "methods": methods, "refs": refs,
            "path": path,
            "disallow_multiple": disallow_multiple,
        })
    return {
        "path": path,
        "apis": apis,
        "spawns": spawns,
        "uses_z": uses_z,
        "writes_pos": writes_pos,
        "keyboard_keys": keyboard_keys,
        "getcomponent_types": getcomponent_types,
        "addcomponent_types": addcomponent_types,
        "classes": classes,
        "literals": [int(x) for x in re.findall(r"(?<![\w.])(\d+)", scan)
                     if int(x) < 1 << 20],
    }


_PRIM = ("int", "float", "bool", "byte", "short", "uint", "long",
         "double", "sbyte", "ushort", "ulong")


def _blank_method_bodies(bscan):
    """Replace method interiors with spaces so locals are not seen as fields."""
    import tools.cpprust as cpprust
    out = list(bscan)
    for m in re.finditer(
            r"(?m)^[ \t]*(?:public|private|protected|internal)?"
            r"[ \t]*(?:static[ \t]+)?(?:override[ \t]+)?(?:virtual[ \t]+)?"
            r"[\w.<>]+[ \t]+\w+[ \t]*\([^)]*\)\s*\{",
            bscan):
        open_i = m.end() - 1
        close = cpprust._match_brace(bscan, open_i)
        if close is None:
            continue
        for i in range(open_i + 1, close):
            if out[i] not in "\n\r":
                out[i] = " "
    return "".join(out)


def _fields_in(body, bscan):
    """Instance fields; methods (those with `(`) are skipped."""
    bscan = _blank_method_bodies(bscan)
    out = []
    for m in re.finditer(
            r"(?m)^[ \t]*(?:public|private|protected|internal)?"
            r"[ \t]*(?:static[ \t]+)?(?:readonly[ \t]+)?"
            r"([\w.<>]+)[ \t]+(\w+)[ \t]*(?:=|;)",
            bscan):
        after = bscan[m.end(2):m.end(2) + 8]
        # `int F(` is a method.
        tail = body[m.end(2):m.end(2) + 16]
        if "(" in tail.split(";")[0] and "=" not in tail.split(";")[0]:
            continue
        ty, name = m.group(1).strip(), m.group(2)
        if ty in ("if", "for", "return", "new"):
            continue
        out.append({"ty": ty, "name": name})
    return out


def _methods_in(body, bscan):
    out = []
    for m in re.finditer(
            r"(?m)^[ \t]*(?:public|private|protected|internal)?"
            r"[ \t]*(?:static[ \t]+)?(?:override[ \t]+)?(?:virtual[ \t]+)?"
            r"([\w.<>]+)[ \t]+(\w+)[ \t]*\(([^)]*)\)\s*\{",
            bscan):
        open_i = m.end() - 1
        # _match_brace lives on cpprust; cs2cpp uses it via import.
        import tools.cpprust as cpprust
        close = cpprust._match_brace(bscan, open_i)
        if close is None:
            continue
        src = body[m.start():m.start() + (close - m.start()) + 1]
        impl = body[m.end():m.start() + (close - m.start())]
        out.append({
            "ret": m.group(1).strip(),
            "name": m.group(2),
            "args": m.group(3).strip(),
            "body": impl,
            "src": src,
        })
    return out


# ---------------------------------------------------------------------------
# Layout plan
# ---------------------------------------------------------------------------

def _f16_bits(f):
    """IEEE-754 binary16 bits. Enough for static positions; not a libm."""
    import struct
    # round-trip via float32 then a common half encoding
    sign = 0
    if f < 0:
        sign = 1
        f = -f
    if f == 0.0:
        return sign << 15
    import math
    if math.isinf(f) or math.isnan(f):
        return (sign << 15) | 0x7C00
    exp = 0
    while f >= 2.0 and exp < 15:
        f *= 0.5
        exp += 1
    while f < 1.0 and exp > -14:
        f *= 2.0
        exp -= 1
    mant = int(round((f - 1.0) * 1024.0)) if exp > -14 else int(round(f * 1024.0))
    if mant == 1024:
        mant = 0
        exp += 1
    be = exp + 15
    if be <= 0:
        return sign << 15
    if be >= 31:
        return (sign << 15) | 0x7C00
    return (sign << 15) | (be << 10) | (mant & 1023)


def _bitwidth(lo, hi):
    span = max(abs(int(lo)), abs(int(hi)))
    if span <= 1:
        return 1
    if span <= 7:
        return 3
    if span <= 15:
        return 4
    if span <= 255:
        return 8
    if span <= 65535:
        return 16
    return 32


def plan_layouts(objects, analyses, two_d=None):
    """Per-class packed field list + index width."""
    by_class = {}
    for o in objects:
        by_class.setdefault(o["class"], []).append(o)

    spawn = any(a["spawns"] for a in analyses)
    uses_z = any(a["uses_z"] for a in analyses)
    if two_d is None:
        two_d = (not uses_z) and all(abs(o["pos"][2]) < 1e-6 for o in objects)

    writes = {}
    for a in analyses:
        for c in a["classes"]:
            writes[c["name"]] = writes.get(c["name"], False) or a["writes_pos"]

    for cname, insts in by_class.items():
        # Authored Rigidbody / Animation integrates into transform — writable.
        if any(o.get("rigidbody2d") or o.get("rigidbody")
               or o.get("anim_player") for o in insts):
            writes[cname] = True

    plans = {}
    for cname, insts in by_class.items():
        n = len(insts)
        bounded = (not spawn) and n > 0
        if bounded and n <= 256:
            idx_ty, idx_bits = "uint8_t", 8
        elif bounded and n <= 65536:
            idx_ty, idx_bits = "uint16_t", 16
        else:
            idx_ty, idx_bits = "uint32_t", 32
            bounded = False

        # Used user fields: union of script fields and scene-serialized names.
        field_tys = {}
        script_fields = []
        for a in analyses:
            for c in a["classes"]:
                if c["name"] == cname:
                    script_fields = c["fields"]
        for f in script_fields:
            field_tys[f["name"]] = f["ty"]
        for o in insts:
            for k in o["fields"]:
                field_tys.setdefault(k, "int")

        static = (not writes.get(cname, False)) and (not spawn)
        members = []

        # Position first — hottest field in scripted scenes.
        if two_d:
            if static:
                members.append(("pos_x", "uint16_t", 16, "f16"))
                members.append(("pos_y", "uint16_t", 16, "f16"))
            else:
                members.append(("pos_x", "float", 32, "f32"))
                members.append(("pos_y", "float", 32, "f32"))
        else:
            members.append(("pos_x", "float", 32, "f32"))
            members.append(("pos_y", "float", 32, "f32"))
            members.append(("pos_z", "float", 32, "f32"))

        for fname, ty in field_tys.items():
            if ty == "Vector2":
                members.append((fname + "_x", "float", 32, "f32"))
                members.append((fname + "_y", "float", 32, "f32"))
                continue
            if ty == "Vector3":
                continue  # transform owns position; full Vector3 fields later
            if ty in ("int", "byte", "short", "uint"):
                vals = [o["fields"][fname] for o in insts if fname in o["fields"]]
                if not vals:
                    vals = [0]
                w = _bitwidth(min(vals), max(vals))
                if w < 8:
                    members.append((fname, "unsigned", w, "bits"))
                elif w == 8:
                    members.append((fname, "uint8_t", 8, "u8"))
                elif w == 16:
                    members.append((fname, "uint16_t", 16, "u16"))
                else:
                    members.append((fname, "int", 32, "i32"))
            elif ty == "bool":
                members.append((fname, "unsigned", 1, "bits"))
            elif ty == "float":
                if static:
                    members.append((fname, "uint16_t", 16, "f16"))
                else:
                    members.append((fname, "float", 32, "f32"))
            else:
                # Foreign MonoBehaviour → index into that class's array.
                members.append((fname, idx_ty, idx_bits, "idx:" + ty))

        # Size with C bitfield packing (same word until 32 bits).
        size = _packed_size(members)
        vec2_fields = [f["name"] for f in script_fields if f["ty"] == "Vector2"]
        plans[cname] = {
            "name": cname,
            "n": n,
            "idx_ty": idx_ty,
            "idx_bits": idx_bits,
            "bounded": bounded,
            "static": static,
            "two_d": two_d,
            "members": members,
            "size": size,
            "instances": insts,
            "fields": script_fields,
            "vec2_fields": vec2_fields,
        }
    return {"two_d": two_d, "spawn": spawn, "classes": plans}


def _packed_size(members):
    """Byte size of a C struct with the bitfields packed as gcc does."""
    byte = 0
    bit_acc = 0
    for _n, ty, bits, kind in members:
        if kind == "bits":
            bit_acc += bits
            while bit_acc >= 32:
                byte += 4
                bit_acc -= 32
        else:
            if bit_acc:
                byte += (bit_acc + 7) // 8
                bit_acc = 0
                byte = (byte + 3) & ~3
            align = 4 if bits >= 32 else (2 if bits == 16 else 1)
            if bits == 32:
                align = 4
            byte = (byte + align - 1) & ~(align - 1)
            byte += bits // 8
    if bit_acc:
        byte += (bit_acc + 7) // 8
    # gcc aligns the struct to the largest member (usually 4).
    return (byte + 3) & ~3


# ---------------------------------------------------------------------------
# C emit
# ---------------------------------------------------------------------------

def _c_ident(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def apply_soa_layout(plan, vec4=False):
    """Move positions out of AoS structs into contiguous float SoA arrays.

    Matches the faster-than-Unity upload idea: GPU position upload reads a
    packed float table, not scattered fields inside object structs. Opt-in
    via --soa / --soa-vec4 so AoS remains the default for size-focused packs.

    vec4=True stores float[N][4] (xyz + instance id in w) so a std140 UBO
    of vec4 matches the CPU table without manual padding.
    """
    plan = dict(plan)
    plan["soa"] = True
    plan["soa_vec4"] = bool(vec4)
    classes = {}
    for cname, cl in plan["classes"].items():
        cl = dict(cl)
        logical = 2 if cl["two_d"] else 3
        pos_names = ("pos_x", "pos_y", "pos_z")[:logical]
        cl["members"] = [m for m in cl["members"] if m[0] not in pos_names]
        cl["soa_logical"] = logical
        cl["soa_dims"] = 4 if vec4 else logical
        cl["size"] = _packed_size(cl["members"])
        classes[cname] = cl
    plan["classes"] = classes
    return plan


def _class_has_position(cl):
    if cl.get("soa_dims"):
        return True
    names = {m[0] for m in cl["members"]}
    return "pos_x" in names and "pos_y" in names


def _soa_axis_count(cl):
    """How many of soa_dims are xyz (vs padding / id in .w)."""
    if cl.get("soa_logical"):
        return cl["soa_logical"]
    if cl.get("soa_dims"):
        return cl["soa_dims"]
    return 2 if cl["two_d"] else 3


def emit_engine(plan, analyses, used_apis):
    lines = []
    p = lines.append
    soa = bool(plan.get("soa"))
    want_math = bool(used_apis & {"Mathf.Sin", "Mathf.Cos"})
    getcomponent_types = set()
    for a in analyses:
        getcomponent_types |= set(a.get("getcomponent_types") or [])
    rb2d_list = plan.get("rigidbody2d") or []
    rb3d_list = plan.get("rigidbody") or []
    col2d_list = plan.get("collider2d") or []
    col3d_list = plan.get("collider3d") or []
    anim_plan = plan.get("animation") or {}
    anim_players = anim_plan.get("players") or []
    anim_clips = anim_plan.get("clips") or []
    anim_keys = anim_plan.get("keys") or []
    add_types = set(plan.get("addcomponent_types") or [])
    add_budget = plan.get("addcomponent_budget") or {}
    disallow_multi = set(plan.get("disallow_multiple_types")
                         or _DISALLOW_MULTIPLE_BUILTINS)
    go_has_sprite = set(plan.get("go_has_sprite") or [])
    want_rb2d = (
        bool(rb2d_list)
        or "Rigidbody2D" in getcomponent_types
        or "Rigidbody2D" in used_apis
        or "Rigidbody2D" in add_types)
    want_rb3d = (
        bool(rb3d_list)
        or "Rigidbody" in getcomponent_types
        or "Rigidbody" in add_types)
    want_col2d = bool(col2d_list) or bool(
        add_types & {"BoxCollider2D", "CircleCollider2D"})
    want_col3d = bool(col3d_list) or bool(
        add_types & {"BoxCollider", "SphereCollider"})
    want_anim = bool(anim_players)
    want_add_camera = "Camera" in add_types
    want_add_light = "Light" in add_types
    want_add_sprite = "SpriteRenderer" in add_types
    want_add_any = bool(add_types)
    want_phys = "Physics2D.gravity" in used_apis or want_rb2d
    want_phys3 = "Physics.gravity" in used_apis or want_rb3d
    want_input = bool(used_apis & _WANT_INPUT)
    want_keyboard = "Keyboard.current" in used_apis
    keyboard_keys = set()
    for a in analyses:
        keyboard_keys |= set(a.get("keyboard_keys") or [])
    want_ambient = "RenderSettings.ambientLight" in used_apis or want_add_light
    want_log = bool(used_apis & {"Debug.Log", "print"})
    want_console = "Console.WriteLine" in used_apis
    want_str_plus = "string.+" in used_apis
    want_find = "GameObject.Find" in used_apis
    want_getcomponent = "GetComponent" in used_apis
    want_go_tables = (
        want_find or want_getcomponent or want_rb2d or want_rb3d
        or want_add_any)
    light_n = int(plan.get("light_count") or 0)
    light_cap = light_n + int(add_budget.get("Light") or 0)
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    p("/* generated by tools/unity_pack.py — do not edit */")
    if soa:
        p("/* layout: SoA positions (contiguous float tables for GPU upload) */")
    p("#include <stdint.h>")
    if want_math or want_col2d or want_col3d or want_anim:
        p("#include <math.h>")
    if want_input or want_log or want_find or want_add_any:
        p("#include <string.h>")
    if want_log or want_console or want_str_plus or want_add_any:
        p("#include <stdio.h>")
    if want_log:
        p("#include <stdlib.h>")
        p("#include <errno.h>")
        p("#ifdef _WIN32")
        p("#include <direct.h>")
        p("#define ENGINE_MKDIR(p) _mkdir(p)")
        p("#else")
        p("#include <sys/stat.h>")
        p("#define ENGINE_MKDIR(p) mkdir((p), 0755)")
        p("#endif")
    p("")
    p("/* Types first, then every global. C forbids `extern T a[N]` while")
    p("   T is incomplete, so the arrays wait until the structs exist;")
    p("   the names are all listed here as comments so data.c and any")
    p("   group can find them. */")
    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        p("typedef struct %s %s;" % (idn, idn))
    p("extern float Time_deltaTime;")
    if "Time.time" in used_apis:
        p("extern float Time_time;")
    if ("Time.fixedDeltaTime" in used_apis or want_phys or want_phys3
            or want_rb2d or want_rb3d):
        p("extern float Time_fixedDeltaTime;")
    if want_phys:
        p("extern float Physics2D_gravity_x;")
        p("extern float Physics2D_gravity_y;")
    if want_phys3:
        p("extern float Physics_gravity_x;")
        p("extern float Physics_gravity_y;")
        p("extern float Physics_gravity_z;")
    if want_ambient:
        p("extern float RenderSettings_ambient_r;")
        p("extern float RenderSettings_ambient_g;")
        p("extern float RenderSettings_ambient_b;")
    if plan.get("camera"):
        p("extern float Camera_main_pos_x;")
        p("extern float Camera_main_pos_y;")
        p("extern float Camera_main_pos_z;")
        p("extern float Camera_main_orthographicSize;")
        p("extern float Camera_main_nearClipPlane;")
        p("extern float Camera_main_farClipPlane;")
        p("extern float Camera_main_background_r;")
        p("extern float Camera_main_background_g;")
        p("extern float Camera_main_background_b;")
        p("extern int Camera_main_orthographic;")
    if want_input:
        p("extern float engine_input_axis_Horizontal;")
        p("extern float engine_input_axis_Vertical;")
        p("extern int engine_input_button_Jump;")
        p("extern unsigned char engine_input_key[256];")
    if want_keyboard:
        p("extern int engine_keyboard_connected;")
        for key in sorted(keyboard_keys):
            p("extern int engine_keyboard_%s;" % key)
    if light_cap:
        p("extern int _Light_count;")
        p("extern float _Light_intensity[%d];" % max(1, light_cap))
        p("extern float _Light_color_r[%d];" % max(1, light_cap))
        p("extern float _Light_color_g[%d];" % max(1, light_cap))
        p("extern float _Light_color_b[%d];" % max(1, light_cap))
    rb2d_cap = len(rb2d_list) + int(add_budget.get("Rigidbody2D") or 0)
    rb3d_cap = len(rb3d_list) + int(add_budget.get("Rigidbody") or 0)
    if want_rb2d:
        n2 = max(1, rb2d_cap if rb2d_cap else len(rb2d_list) or 1)
        p("extern int _Rigidbody2D_count;")
        p("extern float _Rigidbody2D_vel_x[%d];" % n2)
        p("extern float _Rigidbody2D_vel_y[%d];" % n2)
        p("extern float _Rigidbody2D_gravity_scale[%d];" % n2)
        p("extern float _Rigidbody2D_mass[%d];" % n2)
        p("extern int _Rigidbody2D_body_type[%d];" % n2)
        p("extern int _Rigidbody2D_owner_class[%d];" % n2)
        p("extern int _Rigidbody2D_owner_inst[%d];" % n2)
    if want_rb3d:
        n3 = max(1, rb3d_cap if rb3d_cap else len(rb3d_list) or 1)
        p("extern int _Rigidbody_count;")
        p("extern float _Rigidbody_vel_x[%d];" % n3)
        p("extern float _Rigidbody_vel_y[%d];" % n3)
        p("extern float _Rigidbody_vel_z[%d];" % n3)
        p("extern float _Rigidbody_mass[%d];" % n3)
        p("extern int _Rigidbody_use_gravity[%d];" % n3)
        p("extern int _Rigidbody_owner_class[%d];" % n3)
        p("extern int _Rigidbody_owner_inst[%d];" % n3)
    if want_col2d:
        nc = max(1, len(col2d_list))
        p("extern const int _Collider2D_count;")
        p("extern const int _Collider2D_kind[%d]; /* 0 box 1 circle */" % nc)
        p("extern const int _Collider2D_is_trigger[%d];" % nc)
        p("extern const int _Collider2D_body_type[%d]; /* 0 dyn 1 kin 2 static */"
          % nc)
        p("extern const int _Collider2D_owner_class[%d];" % nc)
        p("extern const int _Collider2D_owner_inst[%d];" % nc)
        p("extern const int _Collider2D_rb2d[%d]; /* -1 if none */" % nc)
        p("extern const float _Collider2D_ox[%d];" % nc)
        p("extern const float _Collider2D_oy[%d];" % nc)
        p("extern const float _Collider2D_hw[%d];" % nc)
        p("extern const float _Collider2D_hh[%d];" % nc)
        p("extern const float _Collider2D_cos[%d];" % nc)
        p("extern const float _Collider2D_sin[%d];" % nc)
        p("extern const float _Collider2D_friction[%d];" % nc)
        p("extern const float _Collider2D_bounciness[%d];" % nc)
        p("extern const int _Collider2D_friction_combine[%d];" % nc)
        p("extern const int _Collider2D_bounce_combine[%d];" % nc)
    if want_col3d:
        n3c = max(1, len(col3d_list))
        p("extern const int _Collider3D_count;")
        p("extern const int _Collider3D_kind[%d]; /* 0 box 1 sphere */" % n3c)
        p("extern const int _Collider3D_is_trigger[%d];" % n3c)
        p("extern const int _Collider3D_body_type[%d]; /* 0 dyn 2 static */"
          % n3c)
        p("extern const int _Collider3D_owner_class[%d];" % n3c)
        p("extern const int _Collider3D_owner_inst[%d];" % n3c)
        p("extern const int _Collider3D_rb3d[%d]; /* -1 if none */" % n3c)
        p("extern const float _Collider3D_ox[%d];" % n3c)
        p("extern const float _Collider3D_oy[%d];" % n3c)
        p("extern const float _Collider3D_oz[%d];" % n3c)
        p("extern const float _Collider3D_hw[%d];" % n3c)
        p("extern const float _Collider3D_hh[%d];" % n3c)
        p("extern const float _Collider3D_hd[%d];" % n3c)
        p("extern const float _Collider3D_dynamic_friction[%d];" % n3c)
        p("extern const float _Collider3D_static_friction[%d];" % n3c)
        p("extern const float _Collider3D_bounciness[%d];" % n3c)
        p("extern const int _Collider3D_friction_combine[%d];" % n3c)
        p("extern const int _Collider3D_bounce_combine[%d];" % n3c)
    if want_anim and anim_players:
        np = max(1, len(anim_players))
        nc = max(1, len(anim_clips))
        nk = max(1, len(anim_keys))
        p("extern const int _AnimPlayer_count;")
        p("extern int _AnimPlayer_playing[%d];" % np)
        p("extern float _AnimPlayer_time[%d];" % np)
        p("extern const float _AnimPlayer_speed[%d];" % np)
        p("extern const int _AnimPlayer_loop[%d];" % np)
        p("extern const int _AnimPlayer_clip[%d];" % np)
        p("extern const int _AnimPlayer_owner_class[%d];" % np)
        p("extern const int _AnimPlayer_owner_inst[%d];" % np)
        p("extern const float _AnimPlayer_rest_x[%d];" % np)
        p("extern const float _AnimPlayer_rest_y[%d];" % np)
        p("extern const float _AnimPlayer_rest_z[%d];" % np)
        p("extern const int _AnimClip_count;")
        p("extern const float _AnimClip_length[%d];" % nc)
        p("extern const int _AnimClip_key_begin[%d];" % nc)
        p("extern const int _AnimClip_key_count[%d];" % nc)
        p("extern const int _AnimKey_count;")
        p("extern const float _AnimKey_t[%d];" % nk)
        p("extern const float _AnimKey_x[%d];" % nk)
        p("extern const float _AnimKey_y[%d];" % nk)
        p("extern const float _AnimKey_z[%d];" % nk)
    p("")

    # Packed structs (positions omitted when SoA).
    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        extra = ""
        if cl.get("soa_dims"):
            dims = cl["soa_dims"]
            logical = _soa_axis_count(cl)
            extra = ", pos SoA float[%d]" % dims
            if dims == 4:
                extra += " (xyz + id)"
        p("/* %s: %d instances, ~%d bytes, %s%s */" % (
            idn, cl["n"], cl["size"],
            "static f16" if cl["static"] else "dynamic f32",
            extra if cl.get("soa_dims") else ""))
        p("struct %s {" % idn)
        if not cl["members"]:
            p("    unsigned _pad : 1; /* empty after SoA split */")
        for name, ty, bits, kind in cl["members"]:
            if kind == "bits":
                p("    %s %s : %d;" % (ty, name, bits))
            else:
                p("    %s %s;" % (ty, name))
        p("};")
        p("")

    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        mb_budget = int((plan.get("addcomponent_budget") or {}).get(cname) or 0)
        cap = cl["n"] + mb_budget
        p("extern %s _%s_inst_array[%d];" % (idn, idn, max(1, cap)))
        if mb_budget:
            p("extern int _%s_inst_count;" % idn)
        else:
            p("extern const int _%s_inst_count;" % idn)
        if cl.get("soa_dims"):
            p("extern float _%s_pos[%d][%d];" % (
                idn, max(1, cap), cl["soa_dims"]))
    p("")

    # Used Unity API only.
    if "Time.deltaTime" in used_apis or "Time.time" in used_apis:
        p("/* Time_* globals are defined in data.c so a host can poke them. */")
    for key, snippet in _API.items():
        if key in used_apis and snippet and snippet is not True:
            p(snippet)
            p("")
    if "Input.GetAxis" in used_apis:
        p("static float Input_GetAxis(const char *name) {")
        p("    if (!name) return 0.f;")
        p("    if (strcmp(name, \"Horizontal\") == 0)")
        p("        return engine_input_axis_Horizontal;")
        p("    if (strcmp(name, \"Vertical\") == 0)")
        p("        return engine_input_axis_Vertical;")
        p("    return 0.f;")
        p("}")
        p("")
    if "Input.GetButton" in used_apis:
        p("static int Input_GetButton(const char *name) {")
        p("    if (name && strcmp(name, \"Jump\") == 0)")
        p("        return engine_input_button_Jump;")
        p("    return 0;")
        p("}")
        p("")
    if "Input.GetKey" in used_apis:
        p("static int Input_GetKey(const char *name) {")
        p("    unsigned char c;")
        p("    if (!name || !name[0]) return 0;")
        p("    c = (unsigned char)name[0];")
        p("    if (c >= 'A' && c <= 'Z') c = (unsigned char)(c - 'A' + 'a');")
        p("    return engine_input_key[c] ? 1 : 0;")
        p("}")
        p("")
    if want_keyboard:
        p("/* Input System Keyboard.current — host sets connected + keys. */")
        p("typedef struct EngineKeyboard { int _pad; } EngineKeyboard;")
        p("static EngineKeyboard _Keyboard_device;")
        p("static EngineKeyboard *Keyboard_current(void) {")
        p("    return engine_keyboard_connected ? &_Keyboard_device : 0;")
        p("}")
        p("")
        for key in sorted(keyboard_keys):
            p("static int Keyboard_%sKey_isPressed(void) {" % key)
            p("    return engine_keyboard_connected && engine_keyboard_%s;"
              % key)
            p("}")
            p("")
    if want_str_plus:
        # C# "" + 1 → "1"; C's ""+1 is pointer arithmetic (often prints garbage).
        p("/* C# string + value (not C pointer arithmetic) */")
        p("static char _engine_str_buf[128];")
        p("static const char *_str_plus_i(const char *a, int b) {")
        p("    snprintf(_engine_str_buf, sizeof _engine_str_buf, \"%s%d\",")
        p("             a ? a : \"\", b);")
        p("    return _engine_str_buf;")
        p("}")
        p("static const char *_str_plus_f(const char *a, float b) {")
        p("    snprintf(_engine_str_buf, sizeof _engine_str_buf, \"%s%g\",")
        p("             a ? a : \"\", (double)b);")
        p("    return _engine_str_buf;")
        p("}")
        p("static const char *_str_plus_s(const char *a, const char *b) {")
        p("    snprintf(_engine_str_buf, sizeof _engine_str_buf, \"%s%s\",")
        p("             a ? a : \"\", b ? b : \"\");")
        p("    return _engine_str_buf;")
        p("}")
        p("#define _str_plus(a, b) _Generic((b), \\")
        p("    int: _str_plus_i, \\")
        p("    float: _str_plus_f, \\")
        p("    double: _str_plus_f, \\")
        p("    default: _str_plus_s \\")
        p(")((a), (b))")
        p("")
    if want_log:
        company = plan.get("company_name") or "DefaultCompany"
        product = plan.get("product_name") or "Player"
        # Unity default: platform Player.log under company/product — not cwd.
        # -logFile - → stdout; -logFile path → that file.
        p("/* Debug.Log / print → Unity Player.log path (not cwd, not stdout) */")
        p("static const char _engine_company[] = %s;" % _c_string(company))
        p("static const char _engine_product[] = %s;" % _c_string(product))
        p("static FILE *_engine_log_fp;")
        p("static int _engine_log_stdout;")
        p("static const char *_engine_log_override; /* NULL = platform default */")
        p("static int _engine_log_opened;")
        p("static char _engine_log_default[1024];")
        p("")
        p("static int _engine_mkdir_p(char *path) {")
        p("    char *p;")
        p("    if (!path || !path[0]) return -1;")
        p("    for (p = path + 1; *p; p++) {")
        p("#ifdef _WIN32")
        p("        if (*p == '/' || *p == '\\\\') {")
        p("#else")
        p("        if (*p == '/') {")
        p("#endif")
        p("            char sep = *p;")
        p("            *p = 0;")
        p("            if (ENGINE_MKDIR(path) != 0 && errno != EEXIST) {")
        p("                *p = sep; return -1;")
        p("            }")
        p("            *p = sep;")
        p("        }")
        p("    }")
        p("    if (ENGINE_MKDIR(path) != 0 && errno != EEXIST) return -1;")
        p("    return 0;")
        p("}")
        p("")
        p("static const char *_engine_default_log_path(void) {")
        p("    const char *home;")
        p("    char dir[1024];")
        p("    int n, i;")
        p("#ifdef _WIN32")
        p("    home = getenv(\"USERPROFILE\");")
        p("    if (!home || !home[0]) home = \".\";")
        p("    n = snprintf(_engine_log_default, sizeof _engine_log_default,")
        p("        \"%s\\\\AppData\\\\LocalLow\\\\%s\\\\%s\\\\Player.log\",")
        p("        home, _engine_company, _engine_product);")
        p("#elif defined(__APPLE__)")
        p("    home = getenv(\"HOME\");")
        p("    if (!home || !home[0]) home = \".\";")
        p("    n = snprintf(_engine_log_default, sizeof _engine_log_default,")
        p("        \"%s/Library/Logs/%s/%s/Player.log\",")
        p("        home, _engine_company, _engine_product);")
        p("#else")
        p("    home = getenv(\"HOME\");")
        p("    if (!home || !home[0]) home = \".\";")
        p("    n = snprintf(_engine_log_default, sizeof _engine_log_default,")
        p("        \"%s/.config/unity3d/%s/%s/Player.log\",")
        p("        home, _engine_company, _engine_product);")
        p("#endif")
        p("    if (n < 0 || (size_t)n >= sizeof _engine_log_default) return 0;")
        p("    if ((size_t)n >= sizeof dir) return 0;")
        p("    for (i = 0; i < n; i++) dir[i] = _engine_log_default[i];")
        p("    dir[n] = 0;")
        p("    for (i = n - 1; i >= 0; i--) {")
        p("#ifdef _WIN32")
        p("        if (dir[i] == '/' || dir[i] == '\\\\') { dir[i] = 0; break; }")
        p("#else")
        p("        if (dir[i] == '/') { dir[i] = 0; break; }")
        p("#endif")
        p("    }")
        p("    if (dir[0]) _engine_mkdir_p(dir);")
        p("    return _engine_log_default;")
        p("}")
        p("")
        p("void engine_set_log_file(const char *path) {")
        p("    if (_engine_log_fp && _engine_log_fp != stdout) {")
        p("        fclose(_engine_log_fp);")
        p("        _engine_log_fp = 0;")
        p("    }")
        p("    _engine_log_opened = 0;")
        p("    if (path && path[0] == '-' && path[1] == 0) {")
        p("        _engine_log_stdout = 1;")
        p("        _engine_log_override = 0;")
        p("    } else if (path && path[0]) {")
        p("        _engine_log_stdout = 0;")
        p("        _engine_log_override = path;")
        p("    } else {")
        p("        _engine_log_stdout = 0;")
        p("        _engine_log_override = 0;")
        p("    }")
        p("}")
        p("")
        p("const char *engine_console_log_path(void) {")
        p("    if (_engine_log_stdout) return \"-\";")
        p("    if (_engine_log_override) return _engine_log_override;")
        p("    return _engine_default_log_path();")
        p("}")
        p("")
        p("static FILE *_engine_log(void) {")
        p("    if (_engine_log_stdout) return stdout;")
        p("    if (!_engine_log_opened) {")
        p("        const char *path;")
        p("        _engine_log_opened = 1;")
        p("        path = _engine_log_override ? _engine_log_override")
        p("                                   : _engine_default_log_path();")
        p("        if (path) _engine_log_fp = fopen(path, \"w\");")
        p("    }")
        p("    return _engine_log_fp;")
        p("}")
        p("")
        p("static void Debug_Log_f(float v) {")
        p("    FILE *f = _engine_log();")
        p("    if (!f) return;")
        p("    fprintf(f, \"%g\\n\", (double)v);")
        p("    fflush(f);")
        p("}")
        p("static void Debug_Log_i(int v) {")
        p("    FILE *f = _engine_log();")
        p("    if (!f) return;")
        p("    fprintf(f, \"%d\\n\", v);")
        p("    fflush(f);")
        p("}")
        p("static void Debug_Log_s(const char *s) {")
        p("    FILE *f = _engine_log();")
        p("    if (!f) return;")
        p("    fputs(s ? s : \"Null\", f);")
        p("    fputc('\\n', f);")
        p("    fflush(f);")
        p("}")
        p("#define Debug_Log(msg) _Generic((msg), \\")
        p("    float: Debug_Log_f, \\")
        p("    double: Debug_Log_f, \\")
        p("    int: Debug_Log_i, \\")
        p("    default: Debug_Log_s \\")
        p(")(msg)")
        p("")
    else:
        p("void engine_set_log_file(const char *path) { (void)path; }")
        p("const char *engine_console_log_path(void) { return \"\"; }")
        p("")
    if want_console:
        p("/* System.Console.WriteLine → stdout (terminal), not Player.log */")
        p("static void Console_WriteLine_f(float v) {")
        p("    printf(\"%g\\n\", (double)v);")
        p("}")
        p("static void Console_WriteLine_i(int v) { printf(\"%d\\n\", v); }")
        p("static void Console_WriteLine_s(const char *s) {")
        p("    puts(s ? s : \"\");")
        p("}")
        p("#define Console_WriteLine(msg) _Generic((msg), \\")
        p("    float: Console_WriteLine_f, \\")
        p("    double: Console_WriteLine_f, \\")
        p("    int: Console_WriteLine_i, \\")
        p("    default: Console_WriteLine_s \\")
        p(")(msg)")
        p("")
    p("void engine_apply_argv(int argc, char **argv) {")
    if want_log:
        p("    int i;")
        p("    for (i = 1; i < argc; i = i + 1) {")
        p("        if (!argv[i]) continue;")
        p("        if ((strcmp(argv[i], \"-logFile\") == 0")
        p("             || strcmp(argv[i], \"-logfile\") == 0)")
        p("            && i + 1 < argc) {")
        p("            engine_set_log_file(argv[i + 1]);")
        p("            i = i + 1;")
        p("        }")
        p("    }")
    else:
        p("    (void)argc; (void)argv;")
    p("}")
    p("")

    if want_go_tables:
        go_names = plan.get("go_names") or []
        go_comps = plan.get("go_components") or {}
        go_rb2d = plan.get("go_rigidbody2d") or {}
        go_rb3d = plan.get("go_rigidbody") or {}
        p("/* GameObject.Find / GetComponent — authored scene tables only */")
        p("static const int _engine_go_count = %d;" % len(go_names))
        if go_names:
            p("static const char *_engine_go_name[%d] = {" % len(go_names))
            for n in go_names:
                p("    %s," % _c_string(n))
            p("};")
        else:
            p("static const char *_engine_go_name[1] = { \"\" };")
        if want_add_any:
            p("static void _engine_cant_add_component("
              "const char *comp, int go) {")
            p("    const char *gon;")
            p("    if (go < 0 || go >= _engine_go_count) gon = \"\";")
            p("    else gon = _engine_go_name[go];")
            p("    fprintf(stderr, \"Can't add component '%s' to %s "
              "because such a component is already added to the "
              "game object!\\n\",")
            p("            comp, gon);")
            p("}")
            p("")
        # Per MonoBehaviour class: instance index at each GO, or -1.
        for cname in sorted(plan["classes"]):
            idn = _c_ident(cname)
            vals = []
            for n in go_names:
                if cname in go_comps.get(n, {}):
                    vals.append(str(go_comps[n][cname]))
                else:
                    vals.append("-1")
            if not vals:
                vals = ["-1"]
            mb_budget = int(add_budget.get(cname) or 0)
            if mb_budget:
                p("static int _engine_go_%s[%d] = { %s };" % (
                    idn, len(vals), ", ".join(vals)))
            else:
                p("static const int _engine_go_%s[%d] = { %s };" % (
                    idn, len(vals), ", ".join(vals)))
            # this instance i → GO index (for GetComponent on this).
            authored_n = int(plan["classes"][cname]["n"])
            cap_n = authored_n + mb_budget
            rev = ["-1"] * max(1, cap_n)
            for n, cmap in go_comps.items():
                if cname in cmap and n in go_names:
                    gi = go_names.index(n)
                    rev[cmap[cname]] = str(gi)
            if mb_budget:
                p("static int _engine_%s_go_of[%d] = { %s };" % (
                    idn, len(rev), ", ".join(rev)))
            else:
                p("static const int _engine_%s_go_of[%d] = { %s };" % (
                    idn, len(rev), ", ".join(rev)))
            p("static int _engine_go_of_%s(unsigned i) {" % idn)
            p("    if (i >= %du) return -1;" % len(rev))
            p("    return _engine_%s_go_of[i];" % idn)
            p("}")
        if want_find:
            p("static int GameObject_Find(const char *name) {")
            p("    int i;")
            p("    if (!name) return -1;")
            p("    for (i = 0; i < _engine_go_count; i = i + 1)")
            p("        if (strcmp(_engine_go_name[i], name) == 0) return i;")
            p("    return -1;")
            p("}")
            p("")
            # Unity prints "null" for a missing Object (Find miss / destroyed).
            p("/* UnityEngine.Object.ToString — \"name (Type)\", else \"null\" */")
            p("static char _object_tostring_buf[256];")
            p("static const char *Object_ToString(int go) {")
            p("    int n;")
            p("    if (go < 0 || go >= _engine_go_count) return \"null\";")
            p("    n = snprintf(_object_tostring_buf, sizeof _object_tostring_buf,")
            p("                 \"%s (UnityEngine.GameObject)\",")
            p("                 _engine_go_name[go]);")
            p("    if (n < 0 || (size_t)n >= sizeof _object_tostring_buf)")
            p("        return _engine_go_name[go];")
            p("    return _object_tostring_buf;")
            p("}")
            p("")
        # Emit GetComponent_<T> for every packed class (and requested types).
        for cname in sorted(set(plan["classes"]) | (
                getcomponent_types - _PHYSICS_COMPONENTS) | (
                add_types - _ADDABLE_BUILTINS)):
            if cname not in plan["classes"]:
                continue
            idn = _c_ident(cname)
            mb_budget = int(add_budget.get(cname) or 0)
            p("static int GameObject_GetComponent_%s(int go) {" % idn)
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_%s[go];" % idn)
            p("}")
            p("")
            if mb_budget:
                cap = int(plan["classes"][cname]["n"]) + mb_budget
                p("static int GameObject_AddComponent_%s(int go) {" % idn)
                p("    int ex;")
                p("    if (go < 0 || go >= _engine_go_count) return -1;")
                p("    ex = _engine_go_%s[go];" % idn)
                if cname in disallow_multi:
                    p("    if (ex >= 0) {")
                    p("        _engine_cant_add_component(\"%s\", go);"
                      % cname)
                    p("        return -1;")
                    p("    }")
                else:
                    p("    if (ex >= 0) return ex;")
                p("    if (_%s_inst_count >= %d) return -1;" % (idn, cap))
                p("    ex = _%s_inst_count;" % idn)
                p("    _%s_inst_count = _%s_inst_count + 1;" % (idn, idn))
                p("    _engine_go_%s[go] = ex;" % idn)
                p("    if (ex >= 0 && ex < %d)" % cap)
                p("        _engine_%s_go_of[ex] = go;" % idn)
                p("    return ex;")
                p("}")
                p("")
                p("static char _%s_tostring_buf[256];" % idn)
                p("static const char *%s_ToString(int ci) {" % idn)
                p("    int n, go;")
                p("    if (ci < 0 || ci >= _%s_inst_count) return \"null\";"
                  % idn)
                p("    go = _engine_%s_go_of[ci];" % idn)
                p("    if (go < 0 || go >= _engine_go_count) return \"null\";")
                p("    n = snprintf(_%s_tostring_buf, sizeof _%s_tostring_buf,"
                  % (idn, idn))
                p("                 \"%%s (%s)\", _engine_go_name[go]);" % cname)
                p("    if (n < 0 || (size_t)n >= sizeof _%s_tostring_buf)"
                  % idn)
                p("        return _engine_go_name[go];")
                p("    return _%s_tostring_buf;" % idn)
                p("}")
                p("")
        if want_rb2d:
            vals = []
            for n in go_names:
                vals.append(str(go_rb2d[n]) if n in go_rb2d else "-1")
            if not vals:
                vals = ["-1"]
            rb2d_add = int(add_budget.get("Rigidbody2D") or 0)
            if rb2d_add:
                p("static int _engine_go_Rigidbody2D[%d] = { %s };" % (
                    len(vals), ", ".join(vals)))
            else:
                p("static const int _engine_go_Rigidbody2D[%d] = { %s };" % (
                    len(vals), ", ".join(vals)))
            p("static int GameObject_GetComponent_Rigidbody2D(int go) {")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_Rigidbody2D[go];")
            p("}")
            p("")
            if rb2d_add:
                p("static int GameObject_AddComponent_Rigidbody2D(int go) {")
                p("    int ex, oi, oc, c;")
                p("    if (go < 0 || go >= _engine_go_count) return -1;")
                p("    ex = _engine_go_Rigidbody2D[go];")
                if "Rigidbody2D" in disallow_multi:
                    p("    if (ex >= 0) {")
                    p("        _engine_cant_add_component(\"Rigidbody2D\", go);")
                    p("        return -1;")
                    p("    }")
                else:
                    p("    if (ex >= 0) return ex;")
                p("    if (_Rigidbody2D_count >= %d) return -1;" % max(1, rb2d_cap))
                p("    ex = _Rigidbody2D_count;")
                p("    _Rigidbody2D_count = _Rigidbody2D_count + 1;")
                p("    _engine_go_Rigidbody2D[go] = ex;")
                p("    _Rigidbody2D_vel_x[ex] = 0.f;")
                p("    _Rigidbody2D_vel_y[ex] = 0.f;")
                p("    _Rigidbody2D_gravity_scale[ex] = 1.f;")
                p("    _Rigidbody2D_mass[ex] = 1.f;")
                p("    _Rigidbody2D_body_type[ex] = 0;")
                p("    oc = -1; oi = 0;")
                for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
                    idn = _c_ident(cname)
                    p("    c = _engine_go_%s[go];" % idn)
                    p("    if (c >= 0) { oc = %d; oi = c; }" % cid)
                p("    _Rigidbody2D_owner_class[ex] = oc;")
                p("    _Rigidbody2D_owner_inst[ex] = oi;")
                p("    return ex;")
                p("}")
                p("")
                p("static char _Rigidbody2D_tostring_buf[256];")
                p("static const char *Rigidbody2D_ToString(int ci) {")
                p("    int n, go;")
                p("    if (ci < 0 || ci >= _Rigidbody2D_count) return \"null\";")
                p("    for (go = 0; go < _engine_go_count; go = go + 1)")
                p("        if (_engine_go_Rigidbody2D[go] == ci) break;")
                p("    if (go >= _engine_go_count) return \"null\";")
                p("    n = snprintf(_Rigidbody2D_tostring_buf,")
                p("                 sizeof _Rigidbody2D_tostring_buf,")
                p("                 \"%s (UnityEngine.Rigidbody2D)\",")
                p("                 _engine_go_name[go]);")
                p("    if (n < 0 || (size_t)n >= sizeof _Rigidbody2D_tostring_buf)")
                p("        return _engine_go_name[go];")
                p("    return _Rigidbody2D_tostring_buf;")
                p("}")
                p("")
        if want_rb3d:
            vals = []
            for n in go_names:
                vals.append(str(go_rb3d[n]) if n in go_rb3d else "-1")
            if not vals:
                vals = ["-1"]
            rb3d_add = int(add_budget.get("Rigidbody") or 0)
            if rb3d_add:
                p("static int _engine_go_Rigidbody[%d] = { %s };" % (
                    len(vals), ", ".join(vals)))
            else:
                p("static const int _engine_go_Rigidbody[%d] = { %s };" % (
                    len(vals), ", ".join(vals)))
            p("static int GameObject_GetComponent_Rigidbody(int go) {")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_Rigidbody[go];")
            p("}")
            p("")
            if rb3d_add:
                p("static int GameObject_AddComponent_Rigidbody(int go) {")
                p("    int ex, oi, oc, c;")
                p("    if (go < 0 || go >= _engine_go_count) return -1;")
                p("    ex = _engine_go_Rigidbody[go];")
                if "Rigidbody" in disallow_multi:
                    p("    if (ex >= 0) {")
                    p("        _engine_cant_add_component(\"Rigidbody\", go);")
                    p("        return -1;")
                    p("    }")
                else:
                    p("    if (ex >= 0) return ex;")
                p("    if (_Rigidbody_count >= %d) return -1;" % max(1, rb3d_cap))
                p("    ex = _Rigidbody_count;")
                p("    _Rigidbody_count = _Rigidbody_count + 1;")
                p("    _engine_go_Rigidbody[go] = ex;")
                p("    _Rigidbody_vel_x[ex] = 0.f;")
                p("    _Rigidbody_vel_y[ex] = 0.f;")
                p("    _Rigidbody_vel_z[ex] = 0.f;")
                p("    _Rigidbody_mass[ex] = 1.f;")
                p("    _Rigidbody_use_gravity[ex] = 1;")
                p("    oc = -1; oi = 0;")
                for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
                    idn = _c_ident(cname)
                    p("    c = _engine_go_%s[go];" % idn)
                    p("    if (c >= 0) { oc = %d; oi = c; }" % cid)
                p("    _Rigidbody_owner_class[ex] = oc;")
                p("    _Rigidbody_owner_inst[ex] = oi;")
                p("    return ex;")
                p("}")
                p("")
                p("static char _Rigidbody_tostring_buf[256];")
                p("static const char *Rigidbody_ToString(int ci) {")
                p("    int n, go;")
                p("    if (ci < 0 || ci >= _Rigidbody_count) return \"null\";")
                p("    for (go = 0; go < _engine_go_count; go = go + 1)")
                p("        if (_engine_go_Rigidbody[go] == ci) break;")
                p("    if (go >= _engine_go_count) return \"null\";")
                p("    n = snprintf(_Rigidbody_tostring_buf,")
                p("                 sizeof _Rigidbody_tostring_buf,")
                p("                 \"%s (UnityEngine.Rigidbody)\",")
                p("                 _engine_go_name[go]);")
                p("    if (n < 0 || (size_t)n >= sizeof _Rigidbody_tostring_buf)")
                p("        return _engine_go_name[go];")
                p("    return _Rigidbody_tostring_buf;")
                p("}")
                p("")

        # Camera / Light / SpriteRenderer / Collider AddComponent pools.
        def _emit_simple_add(type_name, unity_name, budget_key=None,
                             authored_names=None):
            bk = budget_key or type_name
            bud = int(add_budget.get(bk) or 0)
            if type_name not in add_types and not bud:
                return
            if bud <= 0:
                bud = 1
            idn = _c_ident(type_name)
            go_n = max(1, len(go_names))
            authored_names = authored_names or set()
            init_vals = []
            for n in (go_names if go_names else [""]):
                # >=0 marks present (authored sentinel 0).
                init_vals.append("0" if n in authored_names else "-1")
            p("static int _engine_go_%s[%d] = { %s };" % (
                idn, go_n, ", ".join(init_vals)))
            p("static int _%s_live = 0;" % idn)
            p("static const int _%s_cap = %d;" % (idn, bud))
            p("static int _%s_owner_go[%d];" % (idn, bud))
            p("static int GameObject_GetComponent_%s(int go) {" % idn)
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_%s[go];" % idn)
            p("}")
            p("static int GameObject_AddComponent_%s(int go) {" % idn)
            p("    int ex;")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    ex = _engine_go_%s[go];" % idn)
            if type_name in disallow_multi:
                p("    if (ex >= 0) {")
                p("        _engine_cant_add_component(\"%s\", go);"
                  % type_name)
                p("        return -1;")
                p("    }")
            else:
                p("    if (ex >= 0) return ex;")
            p("    if (_%s_live >= _%s_cap) return -1;" % (idn, idn))
            p("    ex = _%s_live;" % idn)
            p("    _%s_live = _%s_live + 1;" % (idn, idn))
            p("    _engine_go_%s[go] = ex;" % idn)
            p("    _%s_owner_go[ex] = go;" % idn)
            p("    return ex;")
            p("}")
            p("static char _%s_tostring_buf[256];" % idn)
            p("static const char *%s_ToString(int ci) {" % idn)
            p("    int n, go;")
            p("    if (ci < 0 || ci >= _%s_live) return \"null\";" % idn)
            p("    go = _%s_owner_go[ci];" % idn)
            p("    if (go < 0 || go >= _engine_go_count) return \"null\";")
            p("    n = snprintf(_%s_tostring_buf, sizeof _%s_tostring_buf,"
              % (idn, idn))
            p("                 \"%%s (%s)\", _engine_go_name[go]);"
              % unity_name)
            p("    if (n < 0 || (size_t)n >= sizeof _%s_tostring_buf)" % idn)
            p("        return _engine_go_name[go];")
            p("    return _%s_tostring_buf;" % idn)
            p("}")
            p("")

        if want_add_camera:
            _emit_simple_add("Camera", "UnityEngine.Camera")
        if want_add_sprite:
            _emit_simple_add("SpriteRenderer", "UnityEngine.SpriteRenderer",
                             authored_names=go_has_sprite)
        if want_add_light:
            # Light also grows the authored light tables when present.
            bud = int(add_budget.get("Light") or 0) or 1
            go_n = max(1, len(go_names))
            p("static int _engine_go_Light[%d];" % go_n)
            p("static int _Light_go_inited = 0;")
            p("static void _Light_ensure_go_map(void) {")
            p("    int i;")
            p("    if (_Light_go_inited) return;")
            p("    _Light_go_inited = 1;")
            p("    for (i = 0; i < _engine_go_count; i = i + 1)")
            p("        _engine_go_Light[i] = -1;")
            p("}")
            p("static int GameObject_GetComponent_Light(int go) {")
            p("    _Light_ensure_go_map();")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_Light[go];")
            p("}")
            p("static int GameObject_AddComponent_Light(int go) {")
            p("    int ex;")
            p("    _Light_ensure_go_map();")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    ex = _engine_go_Light[go];")
            if "Light" in disallow_multi:
                p("    if (ex >= 0) {")
                p("        _engine_cant_add_component(\"Light\", go);")
                p("        return -1;")
                p("    }")
            else:
                p("    if (ex >= 0) return ex;")
            p("    if (_Light_count >= %d) return -1;" % max(1, light_cap))
            p("    ex = _Light_count;")
            p("    _Light_count = _Light_count + 1;")
            p("    _engine_go_Light[go] = ex;")
            p("    _Light_intensity[ex] = 1.f;")
            p("    _Light_color_r[ex] = 1.f;")
            p("    _Light_color_g[ex] = 1.f;")
            p("    _Light_color_b[ex] = 1.f;")
            p("    return ex;")
            p("}")
            p("static char _Light_tostring_buf[256];")
            p("static const char *Light_ToString(int ci) {")
            p("    int n, go;")
            p("    if (ci < 0 || ci >= _Light_count) return \"null\";")
            p("    for (go = 0; go < _engine_go_count; go = go + 1)")
            p("        if (_engine_go_Light[go] == ci) break;")
            p("    if (go >= _engine_go_count) return \"null\";")
            p("    n = snprintf(_Light_tostring_buf, sizeof _Light_tostring_buf,")
            p("                 \"%s (UnityEngine.Light)\", _engine_go_name[go]);")
            p("    if (n < 0 || (size_t)n >= sizeof _Light_tostring_buf)")
            p("        return _engine_go_name[go];")
            p("    return _Light_tostring_buf;")
            p("}")
            p("")
        for col_ty, unity_ty in (
                ("BoxCollider2D", "UnityEngine.BoxCollider2D"),
                ("CircleCollider2D", "UnityEngine.CircleCollider2D"),
                ("BoxCollider", "UnityEngine.BoxCollider"),
                ("SphereCollider", "UnityEngine.SphereCollider")):
            if col_ty in add_types:
                _emit_simple_add(col_ty, unity_ty)

    p("static float f16_to_f32(uint16_t h) {")
    p("    unsigned s = (h >> 15) & 1u;")
    p("    int e = (int)((h >> 10) & 31u) - 15;")
    p("    unsigned m = h & 1023u;")
    p("    float f;")
    p("    if ((h & 0x7fff) == 0) return s ? -0.f : 0.f;")
    p("    f = 1.f + (float)m / 1024.f;")
    p("    while (e > 0) { f = f * 2.f; e = e - 1; }")
    p("    while (e < 0) { f = f * 0.5f; e = e + 1; }")
    p("    return s ? -f : f;")
    p("}")
    p("")

    # Group methods by class; array comment sits on the group.
    methods_by = {}
    for a in analyses:
        for c in a["classes"]:
            methods_by.setdefault(c["name"], []).extend(
                [(c, m) for m in c["methods"]
                 if m["name"] not in ("Start",) or True])

    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        p("/* ---- %s group: instance array is defined in data.c ---- */" % idn)
        p("#define %s_AT(i) (_%s_inst_array[(i)])" % (idn, idn))
        p("")
        # Position accessors: SoA table or AoS fields.
        if cl.get("soa_dims"):
            logical = _soa_axis_count(cl)
            axes = ("pos_x", "pos_y", "pos_z")[:logical]
            for axis_i, axis in enumerate(axes):
                p("static float %s_get_%s(unsigned i) { return _%s_pos[i][%d]; }"
                  % (idn, axis, idn, axis_i))
                p("static void %s_set_%s(unsigned i, float v) { _%s_pos[i][%d] = v; }"
                  % (idn, axis, idn, axis_i))
        # Accessors so generated script C never writes a pointer.
        for name, ty, bits, kind in cl["members"]:
            if kind == "f16":
                p("static float %s_get_%s(unsigned i) { return f16_to_f32(%s_AT(i).%s); }"
                  % (idn, name, idn, name))
            elif kind == "f32":
                p("static float %s_get_%s(unsigned i) { return %s_AT(i).%s; }"
                  % (idn, name, idn, name))
                p("static void %s_set_%s(unsigned i, float v) { %s_AT(i).%s = v; }"
                  % (idn, name, idn, name))
            else:
                p("static unsigned %s_get_%s(unsigned i) { return (unsigned)%s_AT(i).%s; }"
                  % (idn, name, idn, name))
                p("static void %s_set_%s(unsigned i, unsigned v) { %s_AT(i).%s = v; }"
                  % (idn, name, idn, name))
        p("")
        for c, m in methods_by.get(cname, []):
            if m["name"] in ("Awake", "OnEnable"):
                continue
            body = _lower_method_body(m["body"], cl, plan)
            p("static void %s_%s(unsigned i) {" % (idn, m["name"]))
            for line in body.split("\n"):
                if line.strip():
                    p("    " + line.rstrip())
            p("}")
            p("")

        # Tick: Start once (Unity), then FixedUpdate / Update.
        has_start = any(m["name"] == "Start"
                        for _c, m in methods_by.get(cname, []))
        has_fixed = any(m["name"] == "FixedUpdate"
                        for _c, m in methods_by.get(cname, []))
        has_update = any(m["name"] == "Update"
                         for _c, m in methods_by.get(cname, []))
        if has_start:
            p("static int _%s_started = 0;" % idn)
        p("void %s_FixedTick(void) {" % idn)
        if has_fixed:
            p("    int n;")
            p("    for (n = 0; n < _%s_inst_count; n = n + 1)" % idn)
            p("        %s_FixedUpdate((unsigned)n);" % idn)
        else:
            p("    /* no FixedUpdate */")
        p("}")
        p("")
        p("void %s_Tick(void) {" % idn)
        if has_start or has_update:
            p("    int n;")
        if has_start:
            p("    if (!_%s_started) {" % idn)
            p("        _%s_started = 1;" % idn)
            p("        for (n = 0; n < _%s_inst_count; n = n + 1)" % idn)
            p("            %s_Start((unsigned)n);" % idn)
            p("    }")
        if has_update:
            p("    for (n = 0; n < _%s_inst_count; n = n + 1)" % idn)
            p("        %s_Update((unsigned)n);" % idn)
        elif not has_start:
            p("    /* no Update */")
        p("}")
        p("")

    if want_col2d or want_col3d:
        p("/* PhysicsMaterialCombine: Average=0 Multiply=1 Minimum=2 Maximum=3 */")
        p("static float _phys_mat_combine(float a, float b, int ca, int cb) {")
        p("    int mode = ca > cb ? ca : cb;")
        p("    if (mode > 3) mode = 0;")
        p("    if (mode == 1) return a * b;")
        p("    if (mode == 2) return a < b ? a : b;")
        p("    if (mode == 3) return a > b ? a : b;")
        p("    return 0.5f * (a + b);")
        p("}")
        p("")

    if want_col2d and col2d_list:
        p("/* Authored BoxCollider2D / CircleCollider2D — AABB contacts */")
        p("static void _col2d_center(int ci, float *out_x, float *out_y) {")
        p("    unsigned oi = (unsigned)_Collider2D_owner_inst[ci];")
        p("    float px = 0.f, py = 0.f;")
        p("    float c = _Collider2D_cos[ci], s = _Collider2D_sin[ci];")
        p("    float ox = _Collider2D_ox[ci], oy = _Collider2D_oy[ci];")
        p("    switch (_Collider2D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl):
                continue
            p("    case %d:" % cid)
            p("        px = %s_get_pos_x(oi);" % idn)
            p("        py = %s_get_pos_y(oi);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("    *out_x = px + c * ox - s * oy;")
        p("    *out_y = py + s * ox + c * oy;")
        p("}")
        p("")
        p("static void _col2d_set_pos(int ci, float nx, float ny) {")
        p("    unsigned oi = (unsigned)_Collider2D_owner_inst[ci];")
        p("    switch (_Collider2D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl) or cl.get("static"):
                continue
            p("    case %d:" % cid)
            p("        %s_set_pos_x(oi, nx);" % idn)
            p("        %s_set_pos_y(oi, ny);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("}")
        p("")
        p("static void _col2d_get_pos(int ci, float *ox, float *oy) {")
        p("    unsigned oi = (unsigned)_Collider2D_owner_inst[ci];")
        p("    *ox = 0.f; *oy = 0.f;")
        p("    switch (_Collider2D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl):
                continue
            p("    case %d:" % cid)
            p("        *ox = %s_get_pos_x(oi);" % idn)
            p("        *oy = %s_get_pos_y(oi);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("}")
        p("")
        p("static void engine_physics_collide2d(void) {")
        p("    int a, b;")
        p("    for (a = 0; a < _Collider2D_count; a = a + 1) {")
        p("        float ax, ay, bx, by, dx, dy, px, py, ahw, ahh, bhw, bhh;")
        p("        float c, s, sep, tx, ty, nx, ny, vx, vy, vn, vtx, vty, fr, bn, sc;")
        p("        int rb;")
        p("        if (_Collider2D_body_type[a] != 0) continue; /* dynamic only */")
        p("        if (_Collider2D_is_trigger[a]) continue;")
        p("        rb = _Collider2D_rb2d[a];")
        p("        if (rb < 0) continue;")
        p("        _col2d_center(a, &ax, &ay);")
        p("        c = _Collider2D_cos[a]; s = _Collider2D_sin[a];")
        p("        if (_Collider2D_kind[a] == 1) {")
        p("            ahw = _Collider2D_hw[a]; ahh = ahw;")
        p("        } else {")
        p("            ahw = fabsf(c) * _Collider2D_hw[a] + fabsf(s) * _Collider2D_hh[a];")
        p("            ahh = fabsf(s) * _Collider2D_hw[a] + fabsf(c) * _Collider2D_hh[a];")
        p("        }")
        p("        for (b = 0; b < _Collider2D_count; b = b + 1) {")
        p("            if (a == b) continue;")
        p("            if (_Collider2D_is_trigger[b]) continue;")
        p("            _col2d_center(b, &bx, &by);")
        p("            c = _Collider2D_cos[b]; s = _Collider2D_sin[b];")
        p("            if (_Collider2D_kind[b] == 1) {")
        p("                bhw = _Collider2D_hw[b]; bhh = bhw;")
        p("            } else {")
        p("                bhw = fabsf(c) * _Collider2D_hw[b] + fabsf(s) * _Collider2D_hh[b];")
        p("                bhh = fabsf(s) * _Collider2D_hw[b] + fabsf(c) * _Collider2D_hh[b];")
        p("            }")
        p("            dx = ax - bx; dy = ay - by;")
        p("            px = (ahw + bhw) - (dx < 0.f ? -dx : dx);")
        p("            py = (ahh + bhh) - (dy < 0.f ? -dy : dy);")
        p("            if (px <= 0.f || py <= 0.f) continue;")
        p("            _col2d_get_pos(a, &tx, &ty);")
        p("            if (px < py) {")
        p("                sep = (dx < 0.f) ? -px : px;")
        p("                tx = tx + sep;")
        p("                nx = (sep < 0.f) ? -1.f : 1.f; ny = 0.f;")
        p("            } else {")
        p("                sep = (dy < 0.f) ? -py : py;")
        p("                ty = ty + sep;")
        p("                nx = 0.f; ny = (sep < 0.f) ? -1.f : 1.f;")
        p("            }")
        p("            _col2d_set_pos(a, tx, ty);")
        p("            fr = _phys_mat_combine(")
        p("                _Collider2D_friction[a], _Collider2D_friction[b],")
        p("                _Collider2D_friction_combine[a],")
        p("                _Collider2D_friction_combine[b]);")
        p("            bn = _phys_mat_combine(")
        p("                _Collider2D_bounciness[a], _Collider2D_bounciness[b],")
        p("                _Collider2D_bounce_combine[a],")
        p("                _Collider2D_bounce_combine[b]);")
        p("            vx = _Rigidbody2D_vel_x[rb];")
        p("            vy = _Rigidbody2D_vel_y[rb];")
        p("            vn = vx * nx + vy * ny;")
        p("            vtx = vx - vn * nx;")
        p("            vty = vy - vn * ny;")
        p("            if (vn < 0.f) vn = -bn * vn;")
        p("            sc = 1.f - fr;")
        p("            if (sc < 0.f) sc = 0.f;")
        p("            if (sc > 1.f) sc = 1.f;")
        p("            _Rigidbody2D_vel_x[rb] = vtx * sc + vn * nx;")
        p("            _Rigidbody2D_vel_y[rb] = vty * sc + vn * ny;")
        p("            _col2d_center(a, &ax, &ay);")
        p("        }")
        p("    }")
        p("}")
        p("")

    if want_col3d and col3d_list:
        p("/* Authored BoxCollider / SphereCollider — AABB contacts */")
        p("static void _col3d_center(int ci, float *ox, float *oy, float *oz) {")
        p("    unsigned oi = (unsigned)_Collider3D_owner_inst[ci];")
        p("    float px = 0.f, py = 0.f, pz = 0.f;")
        p("    switch (_Collider3D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl):
                continue
            p("    case %d:" % cid)
            p("        px = %s_get_pos_x(oi);" % idn)
            p("        py = %s_get_pos_y(oi);" % idn)
            if not cl.get("two_d"):
                p("        pz = %s_get_pos_z(oi);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("    *ox = px + _Collider3D_ox[ci];")
        p("    *oy = py + _Collider3D_oy[ci];")
        p("    *oz = pz + _Collider3D_oz[ci];")
        p("}")
        p("")
        p("static void _col3d_set_pos(int ci, float nx, float ny, float nz) {")
        p("    unsigned oi = (unsigned)_Collider3D_owner_inst[ci];")
        p("    switch (_Collider3D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl) or cl.get("static"):
                continue
            p("    case %d:" % cid)
            p("        %s_set_pos_x(oi, nx);" % idn)
            p("        %s_set_pos_y(oi, ny);" % idn)
            if not cl.get("two_d"):
                p("        %s_set_pos_z(oi, nz);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("}")
        p("")
        p("static void _col3d_get_pos(int ci, float *ox, float *oy, float *oz) {")
        p("    unsigned oi = (unsigned)_Collider3D_owner_inst[ci];")
        p("    *ox = 0.f; *oy = 0.f; *oz = 0.f;")
        p("    switch (_Collider3D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl):
                continue
            p("    case %d:" % cid)
            p("        *ox = %s_get_pos_x(oi);" % idn)
            p("        *oy = %s_get_pos_y(oi);" % idn)
            if not cl.get("two_d"):
                p("        *oz = %s_get_pos_z(oi);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("}")
        p("")
        p("static void engine_physics_collide3d(void) {")
        p("    int a, b;")
        p("    for (a = 0; a < _Collider3D_count; a = a + 1) {")
        p("        float ax, ay, az, bx, by, bz, dx, dy, dz;")
        p("        float px, py, pz, ahw, ahh, ahd, bhw, bhh, bhd;")
        p("        float sep, tx, ty, tz, nx, ny, nz;")
        p("        float vx, vy, vz, vn, vtx, vty, vtz, fr_d, fr_s, fr, bn, sc, vt_len;")
        p("        int rb;")
        p("        if (_Collider3D_body_type[a] != 0) continue;")
        p("        if (_Collider3D_is_trigger[a]) continue;")
        p("        rb = _Collider3D_rb3d[a];")
        p("        if (rb < 0) continue;")
        p("        _col3d_center(a, &ax, &ay, &az);")
        p("        if (_Collider3D_kind[a] == 1) {")
        p("            ahw = _Collider3D_hw[a]; ahh = ahw; ahd = ahw;")
        p("        } else {")
        p("            ahw = _Collider3D_hw[a];")
        p("            ahh = _Collider3D_hh[a];")
        p("            ahd = _Collider3D_hd[a];")
        p("        }")
        p("        for (b = 0; b < _Collider3D_count; b = b + 1) {")
        p("            if (a == b) continue;")
        p("            if (_Collider3D_is_trigger[b]) continue;")
        p("            _col3d_center(b, &bx, &by, &bz);")
        p("            if (_Collider3D_kind[b] == 1) {")
        p("                bhw = _Collider3D_hw[b]; bhh = bhw; bhd = bhw;")
        p("            } else {")
        p("                bhw = _Collider3D_hw[b];")
        p("                bhh = _Collider3D_hh[b];")
        p("                bhd = _Collider3D_hd[b];")
        p("            }")
        p("            dx = ax - bx; dy = ay - by; dz = az - bz;")
        p("            px = (ahw + bhw) - (dx < 0.f ? -dx : dx);")
        p("            py = (ahh + bhh) - (dy < 0.f ? -dy : dy);")
        p("            pz = (ahd + bhd) - (dz < 0.f ? -dz : dz);")
        p("            if (px <= 0.f || py <= 0.f || pz <= 0.f) continue;")
        p("            _col3d_get_pos(a, &tx, &ty, &tz);")
        p("            nx = 0.f; ny = 0.f; nz = 0.f;")
        p("            if (px <= py && px <= pz) {")
        p("                sep = (dx < 0.f) ? -px : px;")
        p("                tx = tx + sep;")
        p("                nx = (sep < 0.f) ? -1.f : 1.f;")
        p("            } else if (py <= px && py <= pz) {")
        p("                sep = (dy < 0.f) ? -py : py;")
        p("                ty = ty + sep;")
        p("                ny = (sep < 0.f) ? -1.f : 1.f;")
        p("            } else {")
        p("                sep = (dz < 0.f) ? -pz : pz;")
        p("                tz = tz + sep;")
        p("                nz = (sep < 0.f) ? -1.f : 1.f;")
        p("            }")
        p("            _col3d_set_pos(a, tx, ty, tz);")
        p("            fr_d = _phys_mat_combine(")
        p("                _Collider3D_dynamic_friction[a],")
        p("                _Collider3D_dynamic_friction[b],")
        p("                _Collider3D_friction_combine[a],")
        p("                _Collider3D_friction_combine[b]);")
        p("            fr_s = _phys_mat_combine(")
        p("                _Collider3D_static_friction[a],")
        p("                _Collider3D_static_friction[b],")
        p("                _Collider3D_friction_combine[a],")
        p("                _Collider3D_friction_combine[b]);")
        p("            bn = _phys_mat_combine(")
        p("                _Collider3D_bounciness[a], _Collider3D_bounciness[b],")
        p("                _Collider3D_bounce_combine[a],")
        p("                _Collider3D_bounce_combine[b]);")
        p("            vx = _Rigidbody_vel_x[rb];")
        p("            vy = _Rigidbody_vel_y[rb];")
        p("            vz = _Rigidbody_vel_z[rb];")
        p("            vn = vx * nx + vy * ny + vz * nz;")
        p("            vtx = vx - vn * nx;")
        p("            vty = vy - vn * ny;")
        p("            vtz = vz - vn * nz;")
        p("            vt_len = sqrtf(vtx * vtx + vty * vty + vtz * vtz);")
        p("            fr = (vt_len < 0.01f) ? fr_s : fr_d;")
        p("            if (vn < 0.f) vn = -bn * vn;")
        p("            sc = 1.f - fr;")
        p("            if (sc < 0.f) sc = 0.f;")
        p("            if (sc > 1.f) sc = 1.f;")
        p("            _Rigidbody_vel_x[rb] = vtx * sc + vn * nx;")
        p("            _Rigidbody_vel_y[rb] = vty * sc + vn * ny;")
        p("            _Rigidbody_vel_z[rb] = vtz * sc + vn * nz;")
        p("            _col3d_center(a, &ax, &ay, &az);")
        p("        }")
        p("    }")
        p("}")
        p("")

    if want_rb2d or want_rb3d:
        p("/* Authored Rigidbody / Rigidbody2D — gravity + integrate after FixedUpdate */")
        p("static void engine_physics_fixed(void) {")
        p("    int i;")
        if want_rb2d and rb2d_list:
            p("    for (i = 0; i < _Rigidbody2D_count; i = i + 1) {")
            p("        unsigned oi;")
            p("        float vx, vy;")
            p("        if (_Rigidbody2D_body_type[i] != 0) continue; /* Dynamic only */")
            p("        _Rigidbody2D_vel_x[i] = _Rigidbody2D_vel_x[i]")
            p("            + Physics2D_gravity_x * _Rigidbody2D_gravity_scale[i]")
            p("              * Time_fixedDeltaTime;")
            p("        _Rigidbody2D_vel_y[i] = _Rigidbody2D_vel_y[i]")
            p("            + Physics2D_gravity_y * _Rigidbody2D_gravity_scale[i]")
            p("              * Time_fixedDeltaTime;")
            p("        vx = _Rigidbody2D_vel_x[i] * Time_fixedDeltaTime;")
            p("        vy = _Rigidbody2D_vel_y[i] * Time_fixedDeltaTime;")
            p("        oi = (unsigned)_Rigidbody2D_owner_inst[i];")
            p("        switch (_Rigidbody2D_owner_class[i]) {")
            for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
                idn = _c_ident(cname)
                cl = plan["classes"][cname]
                if not _class_has_position(cl) or cl.get("static"):
                    continue
                p("        case %d:" % cid)
                p("            %s_set_pos_x(oi, %s_get_pos_x(oi) + vx);"
                  % (idn, idn))
                p("            %s_set_pos_y(oi, %s_get_pos_y(oi) + vy);"
                  % (idn, idn))
                p("            break;")
            p("        default: break;")
            p("        }")
            p("    }")
        if want_rb3d and rb3d_list:
            p("    for (i = 0; i < _Rigidbody_count; i = i + 1) {")
            p("        unsigned oi;")
            p("        float vx, vy, vz;")
            p("        if (_Rigidbody_use_gravity[i]) {")
            p("            _Rigidbody_vel_x[i] = _Rigidbody_vel_x[i]")
            p("                + Physics_gravity_x * Time_fixedDeltaTime;")
            p("            _Rigidbody_vel_y[i] = _Rigidbody_vel_y[i]")
            p("                + Physics_gravity_y * Time_fixedDeltaTime;")
            p("            _Rigidbody_vel_z[i] = _Rigidbody_vel_z[i]")
            p("                + Physics_gravity_z * Time_fixedDeltaTime;")
            p("        }")
            p("        vx = _Rigidbody_vel_x[i] * Time_fixedDeltaTime;")
            p("        vy = _Rigidbody_vel_y[i] * Time_fixedDeltaTime;")
            p("        vz = _Rigidbody_vel_z[i] * Time_fixedDeltaTime;")
            p("        oi = (unsigned)_Rigidbody_owner_inst[i];")
            p("        switch (_Rigidbody_owner_class[i]) {")
            for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
                idn = _c_ident(cname)
                cl = plan["classes"][cname]
                if not _class_has_position(cl) or cl.get("static"):
                    continue
                p("        case %d:" % cid)
                p("            %s_set_pos_x(oi, %s_get_pos_x(oi) + vx);"
                  % (idn, idn))
                p("            %s_set_pos_y(oi, %s_get_pos_y(oi) + vy);"
                  % (idn, idn))
                if not cl.get("two_d"):
                    p("            %s_set_pos_z(oi, %s_get_pos_z(oi) + vz);"
                      % (idn, idn))
                p("            break;")
            p("        default: break;")
            p("        }")
            p("    }")
        if want_col2d and col2d_list:
            p("    engine_physics_collide2d();")
        if want_col3d and col3d_list:
            p("    engine_physics_collide3d();")
        p("}")
        p("")

    if want_anim and anim_players:
        p("/* Authored Animation / Animator — sample root position curves */")
        p("static float _anim_sample(const float *times, const float *vals,")
        p("                         int begin, int count, float t) {")
        p("    int i;")
        p("    float t0, t1, u;")
        p("    if (count <= 0) return 0.f;")
        p("    if (count == 1) return vals[begin];")
        p("    if (t <= times[begin]) return vals[begin];")
        p("    if (t >= times[begin + count - 1])")
        p("        return vals[begin + count - 1];")
        p("    for (i = 0; i < count - 1; i = i + 1) {")
        p("        t0 = times[begin + i];")
        p("        t1 = times[begin + i + 1];")
        p("        if (t >= t0 && t <= t1) {")
        p("            u = (t1 > t0) ? (t - t0) / (t1 - t0) : 0.f;")
        p("            return vals[begin + i]")
        p("                + (vals[begin + i + 1] - vals[begin + i]) * u;")
        p("        }")
        p("    }")
        p("    return vals[begin + count - 1];")
        p("}")
        p("")
        p("static void engine_animation_tick(void) {")
        p("    int i;")
        p("    for (i = 0; i < _AnimPlayer_count; i = i + 1) {")
        p("        int ci, kb, kc;")
        p("        float t, len, dx, dy, dz, nx, ny, nz;")
        p("        unsigned oi;")
        p("        if (!_AnimPlayer_playing[i]) continue;")
        p("        ci = _AnimPlayer_clip[i];")
        p("        len = _AnimClip_length[ci];")
        p("        if (len <= 0.f) continue;")
        p("        _AnimPlayer_time[i] = _AnimPlayer_time[i]")
        p("            + Time_deltaTime * _AnimPlayer_speed[i];")
        p("        t = _AnimPlayer_time[i];")
        p("        if (_AnimPlayer_loop[i]) {")
        p("            while (t >= len) t = t - len;")
        p("            while (t < 0.f) t = t + len;")
        p("            _AnimPlayer_time[i] = t;")
        p("        } else if (t > len) {")
        p("            t = len;")
        p("            _AnimPlayer_time[i] = t;")
        p("            _AnimPlayer_playing[i] = 0;")
        p("        }")
        p("        kb = _AnimClip_key_begin[ci];")
        p("        kc = _AnimClip_key_count[ci];")
        p("        dx = _anim_sample(_AnimKey_t, _AnimKey_x, kb, kc, t);")
        p("        dy = _anim_sample(_AnimKey_t, _AnimKey_y, kb, kc, t);")
        p("        dz = _anim_sample(_AnimKey_t, _AnimKey_z, kb, kc, t);")
        p("        nx = _AnimPlayer_rest_x[i] + dx;")
        p("        ny = _AnimPlayer_rest_y[i] + dy;")
        p("        nz = _AnimPlayer_rest_z[i] + dz;")
        p("        oi = (unsigned)_AnimPlayer_owner_inst[i];")
        p("        switch (_AnimPlayer_owner_class[i]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl) or cl.get("static"):
                continue
            p("        case %d:" % cid)
            p("            %s_set_pos_x(oi, nx);" % idn)
            p("            %s_set_pos_y(oi, ny);" % idn)
            if not cl.get("two_d"):
                p("            %s_set_pos_z(oi, nz);" % idn)
            p("            break;")
        p("        default: break;")
        p("        }")
        p("    }")
        p("}")
        p("")

    p("void engine_tick(void) {")
    if "Time.time" in used_apis:
        p("    Time_time = Time_time + Time_deltaTime;")
    if want_anim and anim_players:
        p("    engine_animation_tick();")
    for cname in sorted(plan["classes"]):
        p("    %s_FixedTick();" % _c_ident(cname))
    if want_rb2d or want_rb3d:
        p("    engine_physics_fixed();")
    for cname in sorted(plan["classes"]):
        p("    %s_Tick();" % _c_ident(cname))
    p("}")
    p("")
    p("int engine_class_count(void) { return %d; }" % len(plan["classes"]))
    p("")

    # Draw list: authored SpriteRenderer + project PNG only.
    p("/* ---- draw list (SpriteRenderer + texture; see engine_draw.h) ---- */")
    p("typedef struct EngineDraw {")
    p("    float x, y, half_w, half_h;")
    p("    float cos_z, sin_z; /* m_LocalRotation around Z */")
    p("    float r, g, b;")
    p("    int tex; /* index into engine_texture_*; -1 = none */")
    p("} EngineDraw;")
    p("")
    tex_n = len(plan.get("textures") or [])
    p("extern const int _engine_tex_count;")
    if tex_n:
        p("extern const int _engine_tex_w[%d];" % tex_n)
        p("extern const int _engine_tex_h[%d];" % tex_n)
        for ti in range(tex_n):
            p("extern const unsigned char _engine_tex%d_rgba[];" % ti)
    p("")
    p("int engine_texture_count(void) { return _engine_tex_count; }")
    p("")
    p("int engine_texture_width(int id) {")
    if tex_n:
        p("    if (id < 0 || id >= _engine_tex_count) return 0;")
        p("    return _engine_tex_w[id];")
    else:
        p("    (void)id; return 0;")
    p("}")
    p("")
    p("int engine_texture_height(int id) {")
    if tex_n:
        p("    if (id < 0 || id >= _engine_tex_count) return 0;")
        p("    return _engine_tex_h[id];")
    else:
        p("    (void)id; return 0;")
    p("}")
    p("")
    p("const unsigned char *engine_texture_rgba(int id) {")
    if tex_n:
        p("    switch (id) {")
        for ti in range(tex_n):
            p("    case %d: return _engine_tex%d_rgba;" % (ti, ti))
        p("    default: return 0;")
        p("    }")
    else:
        p("    (void)id; return 0;")
    p("}")
    p("")
    p("int engine_collect_draws(EngineDraw *out, int max) {")
    p("    int n = 0;")
    p("    if (!out || max < 1) return 0;")
    any_sprite = False
    has_cam = bool(plan.get("camera"))
    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        if not _class_has_position(cl):
            continue
        spr_idx = []
        for i, o in enumerate(cl["instances"]):
            sp = o.get("sprite")
            if sp and sp.get("enabled", 1) and "tex_id" in sp:
                spr_idx.append((i, sp))
        if not spr_idx:
            continue
        any_sprite = True
        p("    { /* %s SpriteRenderer */" % idn)
        p("        static const float _spr_r[] = { %s };" % ", ".join(
            "%sf" % repr(float(sp["r"])) for _i, sp in spr_idx))
        p("        static const float _spr_g[] = { %s };" % ", ".join(
            "%sf" % repr(float(sp["g"])) for _i, sp in spr_idx))
        p("        static const float _spr_b[] = { %s };" % ", ".join(
            "%sf" % repr(float(sp["b"])) for _i, sp in spr_idx))
        p("        static const float _spr_hw[] = { %s };" % ", ".join(
            "%sf" % repr(float(sp["half_w"])) for _i, sp in spr_idx))
        p("        static const float _spr_hh[] = { %s };" % ", ".join(
            "%sf" % repr(float(sp["half_h"])) for _i, sp in spr_idx))
        p("        static const float _spr_cos[] = { %s };" % ", ".join(
            "%sf" % repr(float(sp.get("cos_z", 1.0))) for _i, sp in spr_idx))
        p("        static const float _spr_sin[] = { %s };" % ", ".join(
            "%sf" % repr(float(sp.get("sin_z", 0.0))) for _i, sp in spr_idx))
        p("        static const int _spr_tex[] = { %s };" % ", ".join(
            str(int(sp["tex_id"])) for _i, sp in spr_idx))
        p("        static const unsigned _spr_i[] = { %s };" % ", ".join(
            str(i) for i, _sp in spr_idx))
        p("        int k;")
        p("        for (k = 0; k < %d && n < max; k = k + 1) {" % len(spr_idx))
        p("            unsigned i = _spr_i[k];")
        # Unity cameras look along +Z (identity). Depth = object_z - cam_z.
        if has_cam:
            if cl.get("two_d"):
                p("            float oz = 0.f;")
            else:
                p("            float oz = %s_get_pos_z(i);" % idn)
            p("            {")
            p("                float depth = oz - Camera_main_pos_z;")
            p("                if (depth < Camera_main_nearClipPlane"
              " || depth > Camera_main_farClipPlane)")
            p("                    continue;")
            p("            }")
        p("            out[n].x = %s_get_pos_x(i);" % idn)
        p("            out[n].y = %s_get_pos_y(i);" % idn)
        p("            out[n].half_w = _spr_hw[k];")
        p("            out[n].half_h = _spr_hh[k];")
        p("            out[n].cos_z = _spr_cos[k];")
        p("            out[n].sin_z = _spr_sin[k];")
        p("            out[n].r = _spr_r[k];")
        p("            out[n].g = _spr_g[k];")
        p("            out[n].b = _spr_b[k];")
        p("            out[n].tex = _spr_tex[k];")
        p("            n = n + 1;")
        p("        }")
        p("    }")
    if not any_sprite:
        p("    /* no authored SpriteRenderers — nothing to draw */")
    p("    return n;")
    p("}")
    p("")

    # Contiguous position upload buffer — SoA is memcpy-friendly; AoS gathers.
    total_floats = 0
    for cl in plan["classes"].values():
        if not _class_has_position(cl):
            continue
        dims = cl.get("soa_dims") or (2 if cl["two_d"] else 3)
        total_floats += cl["n"] * dims
    p("int engine_position_floats(void) { return %d; }" % total_floats)
    p("")
    p("int engine_upload_positions(float *dst, int max_floats) {")
    p("    int n = 0;")
    p("    if (!dst || max_floats < 1) return 0;")
    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        if not _class_has_position(cl):
            continue
        dims = cl.get("soa_dims") or (2 if cl["two_d"] else 3)
        p("    {")
        p("        int i, d;")
        p("        int need = _%s_inst_count * %d;" % (idn, dims))
        p("        if (n + need > max_floats) return n;")
        if cl.get("soa_dims"):
            p("        /* SoA: one contiguous table, no AoS gather */")
            p("        for (i = 0; i < _%s_inst_count; i = i + 1)" % idn)
            p("            for (d = 0; d < %d; d = d + 1)" % dims)
            p("                dst[n + i * %d + d] = _%s_pos[i][d];" % (dims, idn))
        else:
            axes = ("pos_x", "pos_y", "pos_z")[:dims]
            p("        /* AoS gather (Unity-style) */")
            p("        for (i = 0; i < _%s_inst_count; i = i + 1) {" % idn)
            for axis_i, axis in enumerate(axes):
                p("            dst[n + i * %d + %d] = %s_get_%s((unsigned)i);"
                  % (dims, axis_i, idn, axis))
            p("        }")
        p("        n = n + need;")
        p("    }")
    p("    return n;")
    p("}")
    p("")
    return "\n".join(lines) + "\n"


def emit_engine_draw_h():
    """Public draw-list API written next to engine.c so hosts stay in sync."""
    return (
        "/* generated by tools/unity_pack.py — do not edit */\n"
        "#ifndef UNITY_PACK_ENGINE_DRAW_H\n"
        "#define UNITY_PACK_ENGINE_DRAW_H\n"
        "\n"
        "typedef struct EngineDraw {\n"
        "    float x, y, half_w, half_h;\n"
        "    float cos_z, sin_z; /* m_LocalRotation around Z */\n"
        "    float r, g, b;\n"
        "    int tex; /* engine_texture_* index; -1 if none */\n"
        "} EngineDraw;\n"
        "\n"
        "void engine_tick(void);\n"
        "int engine_class_count(void);\n"
        "int engine_collect_draws(EngineDraw *out, int max);\n"
        "int engine_texture_count(void);\n"
        "int engine_texture_width(int id);\n"
        "int engine_texture_height(int id);\n"
        "const unsigned char *engine_texture_rgba(int id); /* RGBA8888 */\n"
        "/* Contiguous x,y[,z] floats for every positioned instance (class\n"
        " * name order). SoA packs fill this from flat tables; AoS gathers. */\n"
        "int engine_position_floats(void);\n"
        "int engine_upload_positions(float *dst, int max_floats);\n"
        "/* Unity -logFile: default platform Player.log; \"-\" = stdout. */\n"
        "void engine_set_log_file(const char *path);\n"
        "void engine_apply_argv(int argc, char **argv);\n"
        "const char *engine_console_log_path(void); /* Application.consoleLogPath */\n"
        "\n"
        "#endif\n"
    )


def _split_call_args(argstr):
    """Split `a, b` or `a, b, c` on commas at paren depth 0."""
    parts = []
    depth = 0
    start = 0
    for i, c in enumerate(argstr):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(argstr[start:i].strip())
            start = i + 1
    parts.append(argstr[start:].strip())
    return parts


def _rewrite_new_vector_assigns(text, idn):
    """`transform.position =/+ = new Vector2/3(...)` with nested calls."""

    def repl_eq(m):
        args = _split_call_args(m.group(1))
        if len(args) < 2:
            return m.group(0)
        return "%s_set_pos_x(i, (%s)); %s_set_pos_y(i, (%s));" % (
            idn, args[0], idn, args[1])

    def repl_add(m):
        args = _split_call_args(m.group(1))
        if len(args) < 2:
            return m.group(0)
        return (
            "%s_set_pos_x(i, %s_get_pos_x(i) + (%s)); "
            "%s_set_pos_y(i, %s_get_pos_y(i) + (%s));" % (
                idn, idn, args[0], idn, idn, args[1])
        )

    flags = re.DOTALL
    text = re.sub(
        r"transform\.position\s*=\s*new\s+Vector2\s*\((.*?)\)\s*;",
        repl_eq, text, flags=flags)
    text = re.sub(
        r"transform\.position\s*=\s*new\s+Vector3\s*\((.*?)\)\s*;",
        repl_eq, text, flags=flags)
    # `+= new Vector2` is CS0034 — refused in _check_csharp_lex.
    text = re.sub(
        r"transform\.position\s*\+=\s*new\s+Vector3\s*\((.*?)\)\s*;",
        repl_add, text, flags=flags)
    return text


def _skip_c_string(text, i):
    """Index just past a C/C# string literal starting at text[i] == '\"'."""
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == '"':
            return j + 1
        j += 1
    return j


def _parse_plus_rhs(text, i):
    """Scan one + operand starting at *i*; stop at top-level + , ) ;."""
    while i < len(text) and text[i] in " \t\n\r":
        i += 1
    start = i
    depth = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            i = _skip_c_string(text, i)
            continue
        if c == "'":
            i += 1
            if i < len(text) and text[i] == "\\":
                i += 2
            elif i < len(text):
                i += 1
            if i < len(text) and text[i] == "'":
                i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                break
            depth -= 1
        elif c in ",;" and depth == 0:
            break
        elif c == "+" and depth == 0:
            break
        i += 1
    return start, i


def _rewrite_string_concat(text):
    """Rewrite C# string + value to _str_plus (C pointer + is wrong).

    Handles `"lit" + expr` and chains via repeated `_str_plus(...) + expr`.
    """
    changed = True
    while changed:
        changed = False
        out = []
        i = 0
        while i < len(text):
            left = None
            left_end = None
            if text.startswith("_str_plus(", i):
                depth = 0
                j = i + len("_str_plus")
                while j < len(text):
                    if text[j] == '"':
                        j = _skip_c_string(text, j)
                        continue
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    j += 1
                left = text[i:j]
                left_end = j
            elif text[i] == '"':
                j = _skip_c_string(text, i)
                left = text[i:j]
                left_end = j
            if left is not None:
                k = left_end
                while k < len(text) and text[k] in " \t\n\r":
                    k += 1
                if k < len(text) and text[k] == "+":
                    rhs_start, rhs_end = _parse_plus_rhs(text, k + 1)
                    rhs = text[rhs_start:rhs_end].strip()
                    if rhs:
                        out.append("_str_plus(%s, (%s))" % (left, rhs))
                        i = rhs_end
                        changed = True
                        continue
            out.append(text[i])
            i += 1
        text = "".join(out)
    return text


def _strip_debug_log_context_arg(text):
    """Debug.Log(msg, context) → Debug_Log(msg); packed builds have no Hierarchy."""
    out = []
    i = 0
    while True:
        m = re.search(r"Debug_Log\s*\(", text[i:])
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:i + m.start()])
        start = i + m.end()  # first char of args
        depth = 1
        j = start
        comma = None
        while j < len(text) and depth:
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            elif c == "," and depth == 1 and comma is None:
                comma = j
            j += 1
        if depth != 0:
            out.append(text[i + m.start():])
            break
        args = text[start:j]
        if comma is not None:
            args = text[start:comma].rstrip()
        out.append("Debug_Log(%s)" % args)
        i = j + 1
    return "".join(out)


def _wrap_log_gameobject_tostring(text):
    """Console/Debug printing a GameObject uses Object.ToString.

    Packed Find returns an int index; printing that int is not Unity. Wrap
    `GameObject_Find(...)` so logs get `name (UnityEngine.GameObject)`.
    """
    out = []
    i = 0
    while True:
        m = re.search(r"(?:Console_WriteLine|Debug_Log)\s*\(", text[i:])
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:i + m.start()])
        call = m.group(0)
        callee = re.match(r"(Console_WriteLine|Debug_Log)", call).group(1)
        start = i + m.end()
        depth = 1
        j = start
        while j < len(text) and depth:
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            out.append(text[i + m.start():])
            break
        args = text[start:j].strip()
        if (re.match(r"GameObject_Find\s*\(", args)
                and not args.startswith("Object_ToString")):
            args = "Object_ToString(%s)" % args
        out.append("%s(%s)" % (callee, args))
        i = j + 1
    return "".join(out)


def _rewrite_csharp_float_literals(text):
    """C# `0f` → C `0.f`. C rejects a float suffix on an integer constant.

    C# allows `0f` / `1F` (digits + real-type-suffix). C needs a decimal
    point (`0.f` / `0.0f`). Literals that already have `.` or an exponent
    (`1.5f`, `1e2f`) are valid in both and left alone. Runs on a blanked
    scan so `"0f"` in a string stays put.
    """
    scan = cs2cpp._blank(text)
    out = []
    pos = 0
    for m in re.finditer(r"(?<![\w.])(\d+)([fF])\b", scan):
        out.append(text[pos:m.start(1)])
        out.append(m.group(1) + "." + m.group(2))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _lower_method_body(body, cl, plan):
    """C# subset method → C against packed arrays.

    `this` / implicit fields become `_Class_inst_array[i].field`.
    `transform.position.x` becomes the packed pos_*. A class-typed
    field is already an index: `other.hp` → `_Other_inst_array[other].hp`.
    """
    idn = _c_ident(cl["name"])
    text = _rewrite_csharp_float_literals(body)
    text = re.sub(r"\bthis\.", "", text)
    text = _rewrite_rigidbody_assigns(text, plan, cl["name"])
    # Find/GetComponent before field rewrites so `.amp` stays on the target type.
    text = _rewrite_find_getcomponent(text, plan, cl["name"])
    text, add_locals = _rewrite_addcomponent(text, plan, cl["name"])
    # API tokens before Vector2 rewrites so nested Mathf.Sin(...) keeps parens.
    text = text.replace("Time.deltaTime", "Time_deltaTime")
    text = text.replace("Time.fixedDeltaTime", "Time_fixedDeltaTime")
    text = text.replace("Time.time", "Time_time")
    text = text.replace("Physics2D.gravity.x", "Physics2D_gravity_x")
    text = text.replace("Physics2D.gravity.y", "Physics2D_gravity_y")
    text = text.replace("Physics.gravity.x", "Physics_gravity_x")
    text = text.replace("Physics.gravity.y", "Physics_gravity_y")
    text = text.replace("Physics.gravity.z", "Physics_gravity_z")
    text = text.replace("RenderSettings.ambientLight.r",
                        "RenderSettings_ambient_r")
    text = text.replace("RenderSettings.ambientLight.g",
                        "RenderSettings_ambient_g")
    text = text.replace("RenderSettings.ambientLight.b",
                        "RenderSettings_ambient_b")
    text = text.replace("Camera.main.orthographicSize",
                        "Camera_main_orthographicSize")
    text = text.replace("Camera.main.transform.position.x",
                        "Camera_main_pos_x")
    text = text.replace("Camera.main.transform.position.y",
                        "Camera_main_pos_y")
    text = text.replace("Camera.main.transform.position.z",
                        "Camera_main_pos_z")
    text = text.replace("Camera.main.nearClipPlane",
                        "Camera_main_nearClipPlane")
    text = text.replace("Camera.main.farClipPlane",
                        "Camera_main_farClipPlane")
    text = re.sub(r"Input\.(GetAxis|GetButton|GetKey)\s*\(",
                  lambda m: "Input_%s(" % m.group(1), text)
    # Keyboard.current.<name>Key.isPressed → helpers (null-safe via connected).
    text = re.sub(
        r"(?:UnityEngine\.InputSystem\.)?Keyboard\.current\.(\w+)Key\.isPressed\b",
        lambda m: "Keyboard_%sKey_isPressed()" % m.group(1),
        text)
    text = re.sub(
        r"(?:UnityEngine\.InputSystem\.)?Keyboard\.current\b",
        "Keyboard_current()", text)
    # Debug.Log / print → Debug_Log. Drop optional context object arg.
    text = re.sub(r"(?:UnityEngine\.)?Debug\.Log\b", "Debug_Log", text)
    text = re.sub(r"(?<![\w.])print\b(?=\s*\()", "Debug_Log", text)
    text = _strip_debug_log_context_arg(text)
    text = re.sub(r"System\.Console\.WriteLine\b", "Console_WriteLine", text)
    text = re.sub(r"(?<![\w.])Console\.WriteLine\b", "Console_WriteLine", text)
    text = _rewrite_string_concat(text)
    # Unity Object.ToString when printing a Find result (name, not index).
    text = _wrap_log_gameobject_tostring(text)
    text = _wrap_log_component_tostring(text, add_locals)
    text = re.sub(r"Mathf\.(Abs|Min|Max|Clamp|Lerp|Sin|Cos)\s*\(",
                  lambda m: "Mathf_%s(" % m.group(1), text)
    text = re.sub(r"transform\.position\.x", idn + "_get_pos_x(i)", text)
    text = re.sub(r"transform\.position\.y", idn + "_get_pos_y(i)", text)
    text = re.sub(r"transform\.position\.z",
                  idn + "_get_pos_z(i)" if not cl["two_d"] else "0.f", text)
    text = _rewrite_new_vector_assigns(text, idn)

    members = {n for n, _t, _b, _k in cl["members"]}
    for vf in cl.get("vec2_fields") or []:
        text = re.sub(r"(?<![_\w])%s\.x\b" % vf, "%s_x" % vf, text)
        text = re.sub(r"(?<![_\w])%s\.y\b" % vf, "%s_y" % vf, text)
        text = re.sub(
            r"(?<![_\w])%s\s*\+=\s*new\s+Vector2\s*\((.*)\)" % vf,
            lambda m, name=vf: (
                (lambda args: (
                    "%s_x = %s_x + (%s); %s_y = %s_y + (%s)" % (
                        name, name, args[0], name, name, args[1])
                    if len(args) >= 2 else m.group(0)
                ))(_split_call_args(m.group(1)))
            ),
            text)
    for name in sorted(members, key=len, reverse=True):
        text = re.sub(
            r"(?<![_\w])%s\s*\+=" % name,
            "%s_set_%s(i, %s_get_%s(i) +" % (idn, name, idn, name),
            text)
        text = re.sub(
            r"(?<![_\w])%s\s*-=" % name,
            "%s_set_%s(i, %s_get_%s(i) -" % (idn, name, idn, name),
            text)
        text = re.sub(
            r"(?<![_\w])%s\s*=" % name,
            "%s_set_%s(i," % (idn, name),
            text)
    # Bare remaining field reads. `(?<![_\w])` skips `Coin_get_hp`.
    for name in sorted(members, key=len, reverse=True):
        text = re.sub(
            r"(?<![_\w])%s(?![\w])" % name,
            "%s_get_%s(i)" % (idn, name),
            text)

    fixed = []
    for line in text.split("\n"):
        if "_set_" in line and line.rstrip().endswith(";"):
            if line.count("(") > line.count(")"):
                line = line.rstrip()[:-1] + ");"
        fixed.append(line)
    text = "\n".join(fixed)

    # Pointer-style `other.hp` where other is an idx member.
    for name, _ty, _bits, kind in cl["members"]:
        if not str(kind).startswith("idx:"):
            continue
        other = kind.split(":", 1)[1]
        oiden = _c_ident(other)
        text = re.sub(
            r"\b%s\.(\w+)" % name,
            lambda m: "%s_AT(%s_get_%s(i)).%s" % (
                oiden, idn, name, m.group(1)),
            text)
    return text


def emit_data(plan, used_apis=None):
    lines = []
    p = lines.append
    used_apis = used_apis or set()
    rb2d_list = plan.get("rigidbody2d") or []
    rb3d_list = plan.get("rigidbody") or []
    add_budget = plan.get("addcomponent_budget") or {}
    add_types = set(plan.get("addcomponent_types") or [])
    rb2d_cap = len(rb2d_list) + int(add_budget.get("Rigidbody2D") or 0)
    rb3d_cap = len(rb3d_list) + int(add_budget.get("Rigidbody") or 0)
    light_budget = int(add_budget.get("Light") or 0)
    want_phys = "Physics2D.gravity" in used_apis or bool(rb2d_list) or (
        "Rigidbody2D" in add_types)
    want_phys3 = "Physics.gravity" in used_apis or bool(rb3d_list) or (
        "Rigidbody" in add_types)
    want_input = bool(used_apis & _WANT_INPUT)
    want_keyboard = "Keyboard.current" in used_apis
    keyboard_keys = set(plan.get("keyboard_keys") or [])
    want_ambient = "RenderSettings.ambientLight" in used_apis or (
        "Light" in add_types)
    lights = plan.get("lights") or []
    light_cap = len(lights) + light_budget
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    p("/* generated by tools/unity_pack.py — scene tables, compile -O0 */")
    if plan.get("soa"):
        p("/* SoA: positions live in _Class_pos[N][dims], not in the struct */")
    p("#include <stdint.h>")
    p("")
    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        p("typedef struct %s %s;" % (idn, idn))
        p("struct %s;" % idn)
    p("")
    # Repeat struct layouts so data.c compiles alone.
    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        p("struct %s {" % idn)
        if not cl["members"]:
            p("    unsigned _pad : 1;")
        for name, ty, bits, kind in cl["members"]:
            if kind == "bits":
                p("    %s %s : %d;" % (ty, name, bits))
            else:
                p("    %s %s;" % (ty, name))
        p("};")
        p("")

    p("float Time_deltaTime = 0.0166667f;")
    if "Time.time" in used_apis:
        p("float Time_time = 0.f;")
    if ("Time.fixedDeltaTime" in used_apis or want_phys or want_phys3
            or rb2d_list or rb3d_list):
        p("float Time_fixedDeltaTime = 0.02f;")
    if want_phys:
        p("float Physics2D_gravity_x = 0.f;")
        p("float Physics2D_gravity_y = -9.81f;")
    if want_phys3:
        p("float Physics_gravity_x = 0.f;")
        p("float Physics_gravity_y = -9.81f;")
        p("float Physics_gravity_z = 0.f;")
    if want_ambient:
        # Unity default ambient-ish grey; host may override.
        p("float RenderSettings_ambient_r = 0.2f;")
        p("float RenderSettings_ambient_g = 0.2f;")
        p("float RenderSettings_ambient_b = 0.2f;")
    cam = plan.get("camera")
    if cam:
        p("float Camera_main_pos_x = %sf;" % repr(float(cam["pos"][0])))
        p("float Camera_main_pos_y = %sf;" % repr(float(cam["pos"][1])))
        p("float Camera_main_pos_z = %sf;" % repr(float(cam["pos"][2])))
        p("float Camera_main_orthographicSize = %sf;" % repr(
            float(cam["orthographic_size"])))
        p("float Camera_main_nearClipPlane = %sf;" % repr(
            float(cam.get("near_clip", 0.3))))
        p("float Camera_main_farClipPlane = %sf;" % repr(
            float(cam.get("far_clip", 1000.0))))
        p("float Camera_main_background_r = %sf;" % repr(float(cam["bg_r"])))
        p("float Camera_main_background_g = %sf;" % repr(float(cam["bg_g"])))
        p("float Camera_main_background_b = %sf;" % repr(float(cam["bg_b"])))
        p("int Camera_main_orthographic = %d;" % int(cam["orthographic"]))
    if want_input:
        p("float engine_input_axis_Horizontal = 0.f;")
        p("float engine_input_axis_Vertical = 0.f;")
        p("int engine_input_button_Jump = 0;")
        p("unsigned char engine_input_key[256]; /* host zeros / sets */")
    if want_keyboard:
        # Host sets connected=1 when a keyboard is present (GLFW: always).
        p("int engine_keyboard_connected = 0;")
        for key in sorted(keyboard_keys):
            p("int engine_keyboard_%s = 0;" % key)
    if light_cap:
        p("int _Light_count = %d;" % len(lights))
        intens = [float(L["intensity"]) for L in lights] + [1.0] * light_budget
        cr = [float(L["r"]) for L in lights] + [1.0] * light_budget
        cg = [float(L["g"]) for L in lights] + [1.0] * light_budget
        cb = [float(L["b"]) for L in lights] + [1.0] * light_budget
        p("float _Light_intensity[%d] = { %s };" % (
            light_cap,
            ", ".join("%sf" % repr(v) for v in intens)))
        p("float _Light_color_r[%d] = { %s };" % (
            light_cap, ", ".join("%sf" % repr(v) for v in cr)))
        p("float _Light_color_g[%d] = { %s };" % (
            light_cap, ", ".join("%sf" % repr(v) for v in cg)))
        p("float _Light_color_b[%d] = { %s };" % (
            light_cap, ", ".join("%sf" % repr(v) for v in cb)))
    if rb2d_cap:
        n = len(rb2d_list)
        cap = rb2d_cap
        p("int _Rigidbody2D_count = %d;" % n)
        def _pad_f(vals, fill=0.0):
            return vals + [fill] * (cap - len(vals))
        def _pad_i(vals, fill=0):
            return vals + [fill] * (cap - len(vals))
        p("float _Rigidbody2D_vel_x[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f(
                [r["vel_x"] for r in rb2d_list]))))
        p("float _Rigidbody2D_vel_y[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f(
                [r["vel_y"] for r in rb2d_list]))))
        p("float _Rigidbody2D_gravity_scale[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f(
                [r["gravity_scale"] for r in rb2d_list], 1.0))))
        p("float _Rigidbody2D_mass[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f(
                [r["mass"] for r in rb2d_list], 1.0))))
        p("int _Rigidbody2D_body_type[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i(
                [r["body_type"] for r in rb2d_list]))))
        p("int _Rigidbody2D_owner_class[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i(
                [class_ids[r["owner_class"]] for r in rb2d_list], -1))))
        p("int _Rigidbody2D_owner_inst[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i(
                [r["owner_inst"] for r in rb2d_list]))))
    if rb3d_cap:
        n = len(rb3d_list)
        cap = rb3d_cap
        p("int _Rigidbody_count = %d;" % n)
        def _pad_f3(vals, fill=0.0):
            return vals + [fill] * (cap - len(vals))
        def _pad_i3(vals, fill=0):
            return vals + [fill] * (cap - len(vals))
        p("float _Rigidbody_vel_x[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f3(
                [r["vel_x"] for r in rb3d_list]))))
        p("float _Rigidbody_vel_y[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f3(
                [r["vel_y"] for r in rb3d_list]))))
        p("float _Rigidbody_vel_z[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f3(
                [r["vel_z"] for r in rb3d_list]))))
        p("float _Rigidbody_mass[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f3(
                [r["mass"] for r in rb3d_list], 1.0))))
        p("int _Rigidbody_use_gravity[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i3(
                [r["use_gravity"] for r in rb3d_list], 1))))
        p("int _Rigidbody_owner_class[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i3(
                [class_ids[r["owner_class"]] for r in rb3d_list], -1))))
        p("int _Rigidbody_owner_inst[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i3(
                [r["owner_inst"] for r in rb3d_list]))))
    col2d_list = plan.get("collider2d") or []
    if col2d_list:
        n = len(col2d_list)
        p("const int _Collider2D_count = %d;" % n)
        p("const int _Collider2D_kind[%d] = { %s };" % (
            n, ", ".join(str(int(c["kind"])) for c in col2d_list)))
        p("const int _Collider2D_is_trigger[%d] = { %s };" % (
            n, ", ".join(str(int(c["is_trigger"])) for c in col2d_list)))
        p("const int _Collider2D_body_type[%d] = { %s };" % (
            n, ", ".join(str(int(c["body_type"])) for c in col2d_list)))
        p("const int _Collider2D_owner_class[%d] = { %s };" % (
            n, ", ".join(str(int(c["owner_class_id"])) for c in col2d_list)))
        p("const int _Collider2D_owner_inst[%d] = { %s };" % (
            n, ", ".join(str(int(c["owner_inst"])) for c in col2d_list)))
        p("const int _Collider2D_rb2d[%d] = { %s };" % (
            n, ", ".join(str(int(c["rb2d"])) for c in col2d_list)))
        p("const float _Collider2D_ox[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["ox"])) for c in col2d_list)))
        p("const float _Collider2D_oy[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["oy"])) for c in col2d_list)))
        p("const float _Collider2D_hw[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["hw"])) for c in col2d_list)))
        p("const float _Collider2D_hh[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["hh"])) for c in col2d_list)))
        p("const float _Collider2D_cos[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["cos_z"])) for c in col2d_list)))
        p("const float _Collider2D_sin[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["sin_z"])) for c in col2d_list)))
        p("const float _Collider2D_friction[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["friction"]))
                         for c in col2d_list)))
        p("const float _Collider2D_bounciness[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["bounciness"]))
                         for c in col2d_list)))
        p("const int _Collider2D_friction_combine[%d] = { %s };" % (
            n, ", ".join(str(int(c["friction_combine"]))
                         for c in col2d_list)))
        p("const int _Collider2D_bounce_combine[%d] = { %s };" % (
            n, ", ".join(str(int(c["bounce_combine"])) for c in col2d_list)))
    col3d_list = plan.get("collider3d") or []
    if col3d_list:
        n = len(col3d_list)
        p("const int _Collider3D_count = %d;" % n)
        p("const int _Collider3D_kind[%d] = { %s };" % (
            n, ", ".join(str(int(c["kind"])) for c in col3d_list)))
        p("const int _Collider3D_is_trigger[%d] = { %s };" % (
            n, ", ".join(str(int(c["is_trigger"])) for c in col3d_list)))
        p("const int _Collider3D_body_type[%d] = { %s };" % (
            n, ", ".join(str(int(c["body_type"])) for c in col3d_list)))
        p("const int _Collider3D_owner_class[%d] = { %s };" % (
            n, ", ".join(str(int(c["owner_class_id"])) for c in col3d_list)))
        p("const int _Collider3D_owner_inst[%d] = { %s };" % (
            n, ", ".join(str(int(c["owner_inst"])) for c in col3d_list)))
        p("const int _Collider3D_rb3d[%d] = { %s };" % (
            n, ", ".join(str(int(c["rb3d"])) for c in col3d_list)))
        p("const float _Collider3D_ox[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["ox"])) for c in col3d_list)))
        p("const float _Collider3D_oy[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["oy"])) for c in col3d_list)))
        p("const float _Collider3D_oz[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["oz"])) for c in col3d_list)))
        p("const float _Collider3D_hw[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["hw"])) for c in col3d_list)))
        p("const float _Collider3D_hh[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["hh"])) for c in col3d_list)))
        p("const float _Collider3D_hd[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["hd"])) for c in col3d_list)))
        p("const float _Collider3D_dynamic_friction[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["dynamic_friction"]))
                         for c in col3d_list)))
        p("const float _Collider3D_static_friction[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["static_friction"]))
                         for c in col3d_list)))
        p("const float _Collider3D_bounciness[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["bounciness"]))
                         for c in col3d_list)))
        p("const int _Collider3D_friction_combine[%d] = { %s };" % (
            n, ", ".join(str(int(c["friction_combine"]))
                         for c in col3d_list)))
        p("const int _Collider3D_bounce_combine[%d] = { %s };" % (
            n, ", ".join(str(int(c["bounce_combine"])) for c in col3d_list)))
    anim_plan = plan.get("animation") or {}
    anim_players = anim_plan.get("players") or []
    anim_clips = anim_plan.get("clips") or []
    anim_keys = anim_plan.get("keys") or []
    if anim_players:
        n = len(anim_players)
        p("const int _AnimPlayer_count = %d;" % n)
        p("int _AnimPlayer_playing[%d] = { %s };" % (
            n, ", ".join(str(int(pl["playing"])) for pl in anim_players)))
        p("float _AnimPlayer_time[%d] = { %s };" % (
            n, ", ".join("0.f" for _ in anim_players)))
        p("const float _AnimPlayer_speed[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(pl["speed"]))
                         for pl in anim_players)))
        p("const int _AnimPlayer_loop[%d] = { %s };" % (
            n, ", ".join(str(int(pl["loop"])) for pl in anim_players)))
        p("const int _AnimPlayer_clip[%d] = { %s };" % (
            n, ", ".join(str(int(pl["clip"])) for pl in anim_players)))
        p("const int _AnimPlayer_owner_class[%d] = { %s };" % (
            n, ", ".join(str(int(pl["owner_class_id"]))
                         for pl in anim_players)))
        p("const int _AnimPlayer_owner_inst[%d] = { %s };" % (
            n, ", ".join(str(int(pl["owner_inst"])) for pl in anim_players)))
        p("const float _AnimPlayer_rest_x[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(pl["rest_x"]))
                         for pl in anim_players)))
        p("const float _AnimPlayer_rest_y[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(pl["rest_y"]))
                         for pl in anim_players)))
        p("const float _AnimPlayer_rest_z[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(pl["rest_z"]))
                         for pl in anim_players)))
        nc = len(anim_clips)
        p("const int _AnimClip_count = %d;" % nc)
        p("const float _AnimClip_length[%d] = { %s };" % (
            nc, ", ".join("%sf" % repr(float(c["length"]))
                          for c in anim_clips)))
        p("const int _AnimClip_key_begin[%d] = { %s };" % (
            nc, ", ".join(str(int(c["key_begin"])) for c in anim_clips)))
        p("const int _AnimClip_key_count[%d] = { %s };" % (
            nc, ", ".join(str(int(c["key_count"])) for c in anim_clips)))
        nk = max(1, len(anim_keys))
        keys = anim_keys or [{"t": 0.0, "x": 0.0, "y": 0.0, "z": 0.0}]
        p("const int _AnimKey_count = %d;" % len(keys))
        p("const float _AnimKey_t[%d] = { %s };" % (
            len(keys),
            ", ".join("%sf" % repr(float(k["t"])) for k in keys)))
        p("const float _AnimKey_x[%d] = { %s };" % (
            len(keys),
            ", ".join("%sf" % repr(float(k["x"])) for k in keys)))
        p("const float _AnimKey_y[%d] = { %s };" % (
            len(keys),
            ", ".join("%sf" % repr(float(k["y"])) for k in keys)))
        p("const float _AnimKey_z[%d] = { %s };" % (
            len(keys),
            ", ".join("%sf" % repr(float(k["z"])) for k in keys)))
    textures = plan.get("textures") or []
    p("const int _engine_tex_count = %d;" % len(textures))
    if textures:
        p("const int _engine_tex_w[%d] = { %s };" % (
            len(textures),
            ", ".join(str(int(t["w"])) for t in textures)))
        p("const int _engine_tex_h[%d] = { %s };" % (
            len(textures),
            ", ".join(str(int(t["h"])) for t in textures)))
        for ti, tex in enumerate(textures):
            rgba = tex["rgba"]
            p("/* %s %dx%d RGBA */" % (
                os.path.basename(tex["path"]), tex["w"], tex["h"]))
            p("const unsigned char _engine_tex%d_rgba[%d] = {" % (
                ti, len(rgba)))
            for i in range(0, len(rgba), 16):
                chunk = rgba[i:i + 16]
                p("    %s%s" % (
                    ", ".join(str(b) for b in chunk),
                    "," if i + 16 < len(rgba) else ""))
            p("};")
    p("")
    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        mb_budget = int(add_budget.get(cname) or 0)
        cap = max(1, cl["n"] + mb_budget)
        if mb_budget:
            p("int _%s_inst_count = %d;" % (idn, cl["n"]))
        else:
            p("const int _%s_inst_count = %d;" % (idn, cl["n"]))
        if cl.get("soa_dims"):
            dims = cl["soa_dims"]
            logical = _soa_axis_count(cl)
            p("float _%s_pos[%d][%d] = {" % (idn, cap, dims))
            for ii, o in enumerate(cl["instances"]):
                coords = [float(o["pos"][0]), float(o["pos"][1])]
                if logical >= 3:
                    coords.append(float(o["pos"][2]))
                elif dims >= 3:
                    coords.append(0.0)  # 2D → vec4: z = 0
                if dims == 4:
                    coords.append(float(ii))  # .w = instance index
                while len(coords) < dims:
                    coords.append(0.0)
                parts = ["%sf" % repr(v) for v in coords]
                p("    { %s }, /* %s */" % (", ".join(parts), o["name"]))
            for _pad in range(mb_budget):
                parts = ["0.f"] * dims
                p("    { %s }, /* addcomponent spare */" % (", ".join(parts)))
            p("};")
            p("")
        p("%s _%s_inst_array[%d] = {" % (idn, idn, cap))
        for o in cl["instances"]:
            parts = []
            for name, ty, bits, kind in cl["members"]:
                if name == "pos_x":
                    v = o["pos"][0]
                    parts.append(_init_num(v, kind))
                elif name == "pos_y":
                    v = o["pos"][1]
                    parts.append(_init_num(v, kind))
                elif name == "pos_z":
                    parts.append(_init_num(o["pos"][2], kind))
                elif name in o["fields"]:
                    parts.append(_init_num(o["fields"][name], kind))
                else:
                    parts.append("0")
            if not parts:
                parts = ["0"]
            p("    { %s }, /* %s */" % (", ".join(parts), o["name"]))
        for _pad in range(mb_budget):
            parts = []
            for name, ty, bits, kind in cl["members"]:
                parts.append(_init_num(0, kind) if kind in ("f16", "f32")
                             else "0")
            if not parts:
                parts = ["0"]
            p("    { %s }, /* addcomponent spare */" % (", ".join(parts)))
        p("};")
        p("")
    return "\n".join(lines) + "\n"


def _init_num(v, kind):
    if kind == "f16":
        return "%du" % _f16_bits(float(v))
    if kind == "f32":
        return "%sf" % (repr(float(v)))
    return str(int(v))


def emit_makefile(outdir):
    return (
        "# generated — engine at -O3, data at -O0; main.c is a headless host\n"
        "CC ?= gcc\n"
        "all: game\n"
        "engine.o: engine.c\n"
        "\t$(CC) -O3 -c -o $@ $<\n"
        "data.o: data.c\n"
        "\t$(CC) -O0 -c -o $@ $<\n"
        "main.o: main.c engine_draw.h\n"
        "\t$(CC) -O2 -c -o $@ $<\n"
        "game: engine.o data.o main.o\n"
        "\t$(CC) -O2 -o $@ engine.o data.o main.o -lm\n"
        "clean:\n"
        "\trm -f engine.o data.o main.o game\n"
    )


def emit_main():
    """Headless host so `make` links: tick a second, print draw count."""
    return (
        "/* generated by tools/unity_pack.py — replace for a real host */\n"
        "#include <stdio.h>\n"
        "#include \"engine_draw.h\"\n"
        "\n"
        "extern float Time_deltaTime;\n"
        "\n"
        "int main(int argc, char **argv) {\n"
        "    EngineDraw buf[256];\n"
        "    int i, n;\n"
        "    engine_apply_argv(argc, argv);\n"
        "    Time_deltaTime = 0.0166667f;\n"
        "    for (i = 0; i < 60; i = i + 1)\n"
        "        engine_tick();\n"
        "    n = engine_collect_draws(buf, 256);\n"
        "    printf(\"ticks=60 draws=%d\\n\", n);\n"
        "    return 0; /* draws may be 0 when no SpriteRenderer */\n"
        "}\n"
    )


def emit_shader_compiler(platform):
    """Minimal IR → platform shading language. Subset: position, color, uv."""
    backends = {
        "linux": ("GLSL 330", "void compile_shader(const char *ir) {\n"
                  "    /* ir tokens: position color uv → GLSL 330 */\n"
                  "    (void)ir;\n}\n"),
        "apple": ("Metal", "void compile_shader(const char *ir) {\n"
                  "    /* ir tokens: position color uv → MSL */\n"
                  "    (void)ir;\n}\n"),
        "windows": ("HLSL", "void compile_shader(const char *ir) {\n"
                    "    /* ir tokens: position color uv → HLSL SM 5 */\n"
                    "    (void)ir;\n}\n"),
        "wasm": ("GLSL ES 300", "void compile_shader(const char *ir) {\n"
                 "    /* ir tokens: position color uv → GLSL ES 300 */\n"
                 "    (void)ir;\n}\n"),
    }
    title, body = backends[platform]
    return (
        "/* shader_compiler_%s.c — %s backend for the packed engine.\n"
        " *\n"
        " * Unity HLSL, Godot shading language and Blender OSM do not\n"
        " * share a type system. This file compiles a *subset IR* only:\n"
        " *   position  — clip-space vertex\n"
        " *   color     — interpolated rgba\n"
        " *   uv        — interpolated float2\n"
        " * Anything else is refused by the packer, not silently faked.\n"
        " * Final look is meant to be edited per-platform in a small\n"
        " * editor that writes this same IR, not the source engine's.\n"
        " */\n%s" % (platform, title, body)
    )


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------

def load_project(root):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise PackError("not a directory: %s" % root)
    _progress("scanning %s" % root)
    _progress("reading .meta guid maps")
    assets = _asset_guid_map(root)
    guids = _guid_map(root, asset_guids=assets)
    objects = []
    lights = []
    cameras = []
    _progress("finding .unity scenes")
    scenes = list(_walk_files(root, (".unity",)))
    _progress("parsing %d .unity scene(s)" % len(scenes))
    for si, path in enumerate(scenes):
        _progress("  scene %d/%d %s" % (
            si + 1, len(scenes), os.path.basename(path)))
        objs, scene_lights, scene_cams = parse_unity_yaml(
            _read(path), guid_to_script=guids, asset_guids=assets)
        objects.extend(objs)
        lights.extend(scene_lights)
        cameras.extend(scene_cams)
    for path in _walk_files(root, (".tscn",)):
        objects.extend(parse_godot_tscn(_read(path)))
    for path in _walk_files(root, (".json",)):
        if os.path.basename(path) == "blender_pack.json":
            objects.extend(parse_blender_json(_read(path)))

    scripts = list(_walk_files(root, (".cs",)))
    _progress("analyzing %d script(s)" % len(scripts))
    analyses = []
    for i, p in enumerate(scripts):
        if scripts and ((i + 1) % 25 == 0 or i + 1 == len(scripts)):
            _progress("  scripts %d/%d" % (i + 1, len(scripts)))
        analyses.append(analyze_script(p))
    have = set()
    for a in analyses:
        for c in a["classes"]:
            have.add(c["name"])
    for o in objects:
        if o["class"] not in have:
            analyses.append({
                "path": "<scene:%s>" % o["name"],
                "apis": set(),
                "spawns": False,
                "uses_z": abs(o["pos"][2]) > 1e-6,
                "writes_pos": False,
                "classes": [{
                    "name": o["class"], "kind": "class",
                    "fields": [{"ty": "int", "name": k}
                               for k in o["fields"]],
                    "methods": [], "refs": [], "path": None,
                }],
                "literals": [],
            })
            have.add(o["class"])
    if not objects:
        raise PackError(
            "no scene objects found under %s "
            "(looked for .unity / .tscn / blender_pack.json)" % root)
    _progress("scene objects=%d lights=%d cameras=%d" % (
        len(objects), len(lights), len(cameras)))
    _attach_sprite_textures(objects, assets)
    return objects, analyses, lights, cameras


def emit_soa_positions_glsl(plan):
    """GLSL ES stub: std430 SSBO matching SoA tables (ES 3.1+ / desktop).

    GLES2 hosts keep using VBOs from engine_upload_positions; this file is
    the documented target for a later SSBO path and for --soa-vec4 / std140
    vec4 uploads.
    """
    lines = []
    p = lines.append
    p("/* generated by tools/unity_pack.py — SoA position buffer layout */")
    p("/* OpenGL / GLSL target. Requires SSBO (std430) or upload as vec4[]. */")
    p("#version 310 es")
    p("precision highp float;")
    p("precision highp int;")
    p("")
    if not plan.get("soa"):
        p("/* Pack was AoS — no SoA tables. Use engine_upload_positions gather. */")
        return "\n".join(lines) + "\n"
    stride = 4 if plan.get("soa_vec4") else None
    p("// std430: tight arrays. For std140 UBOs use --soa-vec4 and vec4[].")
    p("layout(std430, binding = 0) readonly buffer PositionSSBO {")
    if plan.get("soa_vec4"):
        p("    vec4 pos[];  // .xyz world, .w instance id")
    else:
        # Per-class tables are separate in C; for a combined upload buffer the
        # host concatenates engine_upload_positions into one float stream.
        p("    float pos[]; // tightly packed xyz from engine_upload_positions")
    p("};")
    p("")
    p("// Example fetch after engine_upload_positions into an SSBO:")
    p("//   vec3 world = pos[i].xyz;          // --soa-vec4")
    p("//   float id    = pos[i].w;")
    p("// or with tight float[] (non-vec4 SoA):")
    p("//   int o = i * STRIDE; vec3 world = vec3(pos[o], pos[o+1], pos[o+2]);")
    if stride:
        p("#define SOA_STRIDE 4")
    else:
        # document per-class strides in comments
        for cname, cl in sorted(plan["classes"].items()):
            if cl.get("soa_dims"):
                p("// %s stride %d (logical xyz %d)" % (
                    _c_ident(cname), cl["soa_dims"], _soa_axis_count(cl)))
    p("")
    return "\n".join(lines) + "\n"


def validate_emitted_c(text, path="engine.c"):
    """Gate generated C through cpprust's subset checks (same as csrust's C++ half).

    unity_pack lowers by hand; this proves the result still sits inside the
    crust subset that `tools/cpprust.py` accepts — `_check_unsupported` plus
    a full `translate` pass. The translated text is discarded; only the
    refusal matters. Raises PackError on subset violations.
    """
    import tools.cpprust as cpprust
    try:
        scan = cpprust._blank_directives(cpprust._strip_comments(text))
        cpprust._check_unsupported(scan, path)
        cpprust.translate(text, path=path)
    except cpprust.CppError as e:
        raise PackError(
            "emitted %s left the crust / cpprust subset: %s"
            % (path, e.message))


def pack(root, outdir, soa=False, soa_vec4=False):
    objects, analyses, lights, cameras = load_project(root)
    used_apis = set()
    for a in analyses:
        used_apis |= a["apis"]
    for api, reason in sorted(_REFUSED_API.items()):
        if api in used_apis:
            raise PackError("%s: %s" % (api, reason))
    add_types = _collect_addcomponent_types(analyses)
    if "Camera.main" in used_apis and not cameras:
        raise PackError(
            "Camera.main: no Camera in the scene — unity_pack does not invent "
            "a default camera. Add an authored Camera (tag MainCamera).")
    _progress("planning layouts (%d objects)" % len(objects))
    plan = plan_layouts(objects, analyses)
    if soa or soa_vec4:
        plan = apply_soa_layout(plan, vec4=bool(soa_vec4))
    else:
        plan = dict(plan)
        plan["soa"] = False
        plan["soa_vec4"] = False
    _validate_addcomponent_types(add_types, plan)
    plan["addcomponent_types"] = sorted(add_types)
    plan["addcomponent_budget"] = _addcomponent_budget(analyses, plan)
    plan["disallow_multiple_types"] = sorted(_disallow_multiple_types(analyses))
    plan["go_has_sprite"] = sorted(_gos_with_sprite(plan))
    plan["lights"] = list(lights)
    plan["light_count"] = len(lights)
    main_cam = None
    for c in cameras:
        if c.get("main"):
            main_cam = c
            break
    if main_cam is None and cameras:
        main_cam = cameras[0]
    plan["camera"] = main_cam
    plan["cameras"] = list(cameras)
    plan["textures"] = _collect_textures(objects)
    kb_keys = set()
    for a in analyses:
        kb_keys |= set(a.get("keyboard_keys") or [])
    plan["keyboard_keys"] = sorted(kb_keys)
    company, product = player_identity(root)
    plan["company_name"] = company
    plan["product_name"] = product
    go_names, go_comps = _build_go_tables(plan)
    plan["go_names"] = go_names
    plan["go_components"] = go_comps
    rb2d, rb3d, go_rb2d, go_rb3d = _build_rigidbody_tables(plan)
    plan["rigidbody2d"] = rb2d
    plan["rigidbody"] = rb3d
    plan["go_rigidbody2d"] = go_rb2d
    plan["go_rigidbody"] = go_rb3d
    plan["collider2d"] = _build_collider2d_tables(plan)
    plan["collider3d"] = _build_collider3d_tables(plan)
    plan["animation"] = _build_animation_tables(plan)
    os.makedirs(outdir, exist_ok=True)
    _progress("emitting engine.c (%d classes)" % len(plan["classes"]))
    engine = emit_engine(plan, analyses, used_apis)
    _progress("emitting data.c (%d texture(s))" % len(plan.get("textures") or []))
    data = emit_data(plan, used_apis)
    main_c = emit_main()
    _progress("validating engine.c through cpprust")
    validate_emitted_c(engine, "engine.c")
    _progress("validating data.c through cpprust")
    validate_emitted_c(data, "data.c")
    _progress("validating main.c through cpprust")
    validate_emitted_c(main_c, "main.c")
    _progress("writing %s" % outdir)
    with open(os.path.join(outdir, "engine.c"), "w") as f:
        f.write(engine)
    with open(os.path.join(outdir, "data.c"), "w") as f:
        f.write(data)
    with open(os.path.join(outdir, "engine_draw.h"), "w") as f:
        f.write(emit_engine_draw_h())
    with open(os.path.join(outdir, "main.c"), "w") as f:
        f.write(main_c)
    with open(os.path.join(outdir, "Makefile"), "w") as f:
        f.write(emit_makefile(outdir))
    shdir = os.path.join(outdir, "shaders")
    os.makedirs(shdir, exist_ok=True)
    for plat in ("linux", "apple", "windows", "wasm"):
        with open(os.path.join(shdir, "shader_compiler_%s.c" % plat),
                  "w") as f:
            f.write(emit_shader_compiler(plat))
    with open(os.path.join(shdir, "soa_positions.glsl"), "w") as f:
        f.write(emit_soa_positions_glsl(plan))
    _progress("done")
    return plan


def main():
    args = list(sys.argv[1:])
    outdir = None
    soa = False
    soa_vec4 = False
    if "--soa-vec4" in args:
        soa_vec4 = True
        soa = True
        args.remove("--soa-vec4")
    if "--soa" in args:
        soa = True
        args.remove("--soa")
    if "-o" in args:
        i = args.index("-o")
        if i + 1 >= len(args):
            sys.stderr.write("unity_pack: -o needs a directory\n")
            return 2
        outdir = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1 or outdir is None:
        sys.stderr.write(
            "usage: unity_pack.py <project-dir> -o <out-dir> "
            "[--soa | --soa-vec4]\n")
        return 2
    try:
        plan = pack(args[0], outdir, soa=soa, soa_vec4=soa_vec4)
    except PackError as e:
        # csc/Unity diagnostics print verbatim; other refusals keep the prefix.
        if ": error CS" in e.message:
            sys.stderr.write("%s\n" % e.message)
        else:
            sys.stderr.write("unity_pack: %s\n" % e.message)
        return 1
    sys.stderr.write(
        "unity_pack: %d classes, 2d=%s, soa=%s, soa_vec4=%s, "
        "wrote %s/{engine.c,data.c,main.c,engine_draw.h}\n"
        % (len(plan["classes"]), plan["two_d"], plan.get("soa"),
           plan.get("soa_vec4"), outdir))
    for name, cl in sorted(plan["classes"].items()):
        extra = ""
        if cl.get("soa_dims"):
            extra = " soa_dims=%d" % cl["soa_dims"]
            if cl.get("soa_logical"):
                extra += " xyz=%d" % cl["soa_logical"]
        sys.stderr.write("  %s n=%d size=%d idx=%s%s\n"
                         % (name, cl["n"], cl["size"], cl["idx_ty"], extra))
    return 0


if __name__ == "__main__":
    sys.exit(main())
