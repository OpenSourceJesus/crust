#!/usr/bin/env python3
"""unity_pack -- emit a packed engine.c + data.c from a Unity-subset project.

See UNITY_PACK.md. Walks scripts and scenes, keeps only the Unity API that
is called, and lays objects out as small as the data allows: drop z in 2D,
float16 for static backgrounds, bitfields for small ints, and uint8_t
indices instead of pointers when a class is bounded (hand-placed, never
spawned, N ≤ 256).

Does not invent scene components (ParticleSystem pools, AnimationCurves,
Rigidbody graphs). It packs and speeds up what the project already authored.

    python3 tools/unity_pack.py <project> -o <outdir>
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cs2cpp as cs2cpp  # noqa: E402


class PackError(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message


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
    "AddComponent<Light>": (
        "Light must be authored on a scene GameObject — unity_pack does not "
        "AddComponent lights."
    ),
    "AddComponent<Camera>": (
        "Camera must be authored on a scene GameObject — unity_pack does not "
        "AddComponent cameras."
    ),
    "AddComponent<SpriteRenderer>": (
        "SpriteRenderer must be authored on a scene GameObject — unity_pack "
        "does not invent default visuals for GameObjects."
    ),
}

_SPAWN = re.compile(
    r"(?<![\w.])(Instantiate|Destroy|Object\.Instantiate|"
    r"GameObject\.Instantiate|new\s+GameObject)\b"
)
_VEC3Z = re.compile(r"\.(z)\b|Vector3|Quaternion")
_UNITY_API = re.compile(
    r"(?:AddComponent\s*<\s*(?:Light|Camera|SpriteRenderer)\s*>|"
    r"(?<![\w])(?:Mathf\.(?:Abs|Min|Max|Clamp|Lerp|Sin|Cos)|"
    r"Time\.(?:deltaTime|time|fixedDeltaTime)|"
    r"Input\.(?:GetAxis|GetButton|GetKey)|"
    r"RenderSettings\.ambientLight|Camera\.main|"
    r"transform\.position|Physics2D\.gravity|ParticleSystem\.Emit|"
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
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "Library", "Temp", "obj",
                                    "graphify-out", "__pycache__")]
        for n in names:
            if any(n.endswith(e) for e in exts):
                out.append(os.path.join(dirpath, n))
    out.sort()
    return out


def _guid_map(root):
    """Unity .meta `guid:` next to a .cs file → script path."""
    out = {}
    for meta in _walk_files(root, (".cs.meta",)):
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
    for meta in _walk_files(root, (".meta",)):
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


def _attach_sprite_textures(objects, asset_guids):
    """Load PNG pixels for each SpriteRenderer that references a project sprite."""
    for o in objects:
        sp = o.get("sprite")
        if not sp:
            continue
        path = asset_guids.get(sp.get("sprite_guid") or "")
        if not path or not path.lower().endswith(".png"):
            o["sprite"] = None
            continue
        try:
            w, h, rgba = _load_png_rgba(path)
        except PackError:
            o["sprite"] = None
            continue
        except IOError:
            o["sprite"] = None
            continue
        sp["tex_path"] = path
        sp["tex_w"] = w
        sp["tex_h"] = h
        sp["tex_rgba"] = rgba


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

    Also imports authored Camera (!u!20) and SpriteRenderer (!u!212). Does
    not invent either — GameObjects without a SpriteRenderer contribute no
    draws. A SpriteRenderer draws only when m_Sprite points at a real
    project asset (guid in asset_guids). Returns (objects, lights, cameras).
    """
    guid_to_script = guid_to_script or {}
    asset_guids = asset_guids or {}
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
            r"(?m)^(GameObject|Transform|MonoBehaviour|PrefabInstance|"
            r"Light|Camera|SpriteRenderer):",
            block)
        if km:
            kind = km.group(1)
        elif type_id == "108":
            kind = "Light"
        elif type_id == "20":
            kind = "Camera"
        elif type_id == "212":
            kind = "SpriteRenderer"
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
            rec["camera"] = {
                "orthographic": int(ortho.group(1)) if ortho else 1,
                "orthographic_size": (
                    float(osize.group(1)) if osize else 5.0),
                "bg_r": float(bg.group(1)) if bg else 0.05,
                "bg_g": float(bg.group(2)) if bg else 0.05,
                "bg_b": float(bg.group(3)) if bg else 0.08,
            }
        by_id[file_id] = rec

    # Join MonoBehaviour + Transform + SpriteRenderer onto the GameObject.
    gos = [r for r in by_id.values() if r.get("kind") == "GameObject"]
    for go in gos:
        kids = []
        for mid in re.findall(r"fileID:\s*(\d+)", go["raw"]):
            if mid in by_id and by_id[mid] is not go:
                kids.append(by_id[mid])
        pos = (0.0, 0.0, 0.0)
        scale = (1.0, 1.0, 1.0)
        script = None
        fields = {}
        sprite = None
        cam = None
        for k in kids:
            if k.get("pos"):
                pos = k["pos"]
            if k.get("scale"):
                scale = k["scale"]
            if k.get("kind") == "MonoBehaviour":
                fields.update(k.get("fields") or {})
                g = k.get("guid")
                if g and g in guid_to_script:
                    script = guid_to_script[g]
            if k.get("kind") == "SpriteRenderer" and k.get("sprite"):
                sprite = dict(k["sprite"])
            if k.get("kind") == "Camera" and k.get("camera"):
                cam = dict(k["camera"])
        if sprite and sprite.get("enabled", 1) and sprite.get("has_sprite"):
            # Size from authored Transform scale (no invented sprite mesh).
            sprite["half_w"] = 0.5 * abs(float(scale[0]))
            sprite["half_h"] = 0.5 * abs(float(scale[1]))
        else:
            sprite = None
        class_name = None
        if script:
            class_name = _class_name_from_cs(script)
        if cam is not None:
            cameras.append({
                "name": go.get("name") or "Camera",
                "pos": pos,
                "main": (go.get("tag") == "MainCamera"
                         or (go.get("name") or "").lower() == "main camera"),
                "orthographic": cam["orthographic"],
                "orthographic_size": cam["orthographic_size"],
                "bg_r": cam["bg_r"],
                "bg_g": cam["bg_g"],
                "bg_b": cam["bg_b"],
            })
        # Camera-only GOs are not packed as scripted instances.
        if cam is not None and script is None and sprite is None:
            continue
        objects.append({
            "name": go.get("name") or "obj",
            "pos": pos,
            "fields": fields,
            "script": script,
            "class": class_name or go.get("name") or "Obj",
            "sprite": sprite,
        })
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
            fm = re.match(r"\s*\.\s*([A-Za-z_]\w*)\b", scan[end:])
            if fm:
                field = fm.group(1)
                end = end + fm.end()
        out.append({
            "start": m.start(),
            "end": end,
            "find_args": find_args.strip(),
            "component": comp_ty,
            "field": field,
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
        after_angle = scan[angle_close + 1:]
        if after_angle.find("(") < 0:
            continue
        g_open = angle_close + 1 + after_angle.find("(")
        g_close = cpprust._match_paren(scan, g_open)
        if g_close is None:
            continue
        end = g_close + 1
        field = None
        fm = re.match(r"\s*\.\s*([A-Za-z_]\w*)\b", scan[end:])
        if fm:
            field = fm.group(1)
            end = end + fm.end()
        out.append({
            "start": m.start(),
            "end": end,
            "find_args": None,  # this GameObject
            "component": comp_ty,
            "field": field,
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


def _rewrite_find_getcomponent(text, plan, this_class):
    """Lower Find/GetComponent chains using the authored GO tables.

    `GameObject.Find` name lookup is always runtime (`strcmp` on the packed
    name table) — missing names yield -1 like Unity null, not a PackError.
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

    def _field_after_get(comp, field, go_expr):
        idn = _c_ident(comp)
        zero = _zero_for_field(comp, field)
        return (
            "({ int _up_gc = GameObject_GetComponent_%s(%s); "
            "_up_gc < 0 ? %s : %s_get_%s((unsigned)_up_gc); })"
            % (idn, go_expr, zero, idn, field)
        )

    for ch in chains:
        comp = ch.get("component")
        field = ch.get("field")
        if ch.get("on_this"):
            if not comp:
                raise PackError("GetComponent requires a type argument")
            this_idn = _c_ident(this_class)
            comp_idn = _c_ident(comp)
            if comp not in (plan.get("classes") or {}):
                raise PackError(
                    "GetComponent<%s>: no authored %s in the scene — "
                    "unity_pack does not invent components" % (comp, comp))
            go_expr = "_engine_go_of_%s(i)" % this_idn
            if field:
                repl = _field_after_get(comp, field, go_expr)
            else:
                repl = "GameObject_GetComponent_%s(%s)" % (comp_idn, go_expr)
            text = text[:ch["start"]] + repl + text[ch["end"]:]
            continue

        find_args = ch.get("find_args") or ""
        # Always runtime — do not pack-time require the name to exist.
        go_expr = "GameObject_Find(%s)" % find_args
        if not comp:
            repl = go_expr
        else:
            if comp not in (plan.get("classes") or {}):
                raise PackError(
                    "GetComponent<%s>: no authored %s in the scene — "
                    "unity_pack does not invent components" % (comp, comp))
            if field:
                repl = _field_after_get(comp, field, go_expr)
            else:
                repl = "GameObject_GetComponent_%s(%s)" % (
                    _c_ident(comp), go_expr)
        text = text[:ch["start"]] + repl + text[ch["end"]:]
    return text


def analyze_script(path, text=None):
    """Fields, methods, Unity API used, whether the script spawns."""
    if text is None:
        text = _read(path)
    scan = cs2cpp._blank(text)
    apis = set()
    for m in _UNITY_API.finditer(scan):
        token = m.group(0)
        if token.startswith("AddComponent"):
            if "Light" in token:
                apis.add("AddComponent<Light>")
            elif "Camera" in token:
                apis.add("AddComponent<Camera>")
            elif "SpriteRenderer" in token:
                apis.add("AddComponent<SpriteRenderer>")
        elif token.startswith("GetComponent"):
            apis.add("GetComponent")
        elif "GameObject.Find" in token or token == "GameObject.Find":
            apis.add("GameObject.Find")
        else:
            apis.add(token)
    # AST pass: precise Find / GetComponent detection (cpprust paren/angle).
    getcomponent_types = set()
    for ch in _ast_find_getcomponent_chains(text):
        if ch.get("find_args") is not None and not ch.get("on_this"):
            apis.add("GameObject.Find")
        if ch.get("component"):
            apis.add("GetComponent")
            getcomponent_types.add(ch["component"])
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
        classes.append({
            "name": name, "kind": kind, "fields": fields,
            "methods": methods, "refs": refs,
            "path": path,
        })
    return {
        "path": path,
        "apis": apis,
        "spawns": spawns,
        "uses_z": uses_z,
        "writes_pos": writes_pos,
        "keyboard_keys": keyboard_keys,
        "getcomponent_types": getcomponent_types,
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
    want_phys = "Physics2D.gravity" in used_apis
    want_input = bool(used_apis & _WANT_INPUT)
    want_keyboard = "Keyboard.current" in used_apis
    keyboard_keys = set()
    for a in analyses:
        keyboard_keys |= set(a.get("keyboard_keys") or [])
    want_ambient = "RenderSettings.ambientLight" in used_apis
    want_log = bool(used_apis & {"Debug.Log", "print"})
    want_console = "Console.WriteLine" in used_apis
    want_find = "GameObject.Find" in used_apis
    want_getcomponent = "GetComponent" in used_apis
    getcomponent_types = set()
    for a in analyses:
        getcomponent_types |= set(a.get("getcomponent_types") or [])
    light_n = int(plan.get("light_count") or 0)
    p("/* generated by tools/unity_pack.py — do not edit */")
    if soa:
        p("/* layout: SoA positions (contiguous float tables for GPU upload) */")
    p("#include <stdint.h>")
    if want_math:
        p("#include <math.h>")
    if want_input or want_log or want_find:
        p("#include <string.h>")
    if want_log or want_console:
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
    if "Time.fixedDeltaTime" in used_apis or want_phys:
        p("extern float Time_fixedDeltaTime;")
    if want_phys:
        p("extern float Physics2D_gravity_x;")
        p("extern float Physics2D_gravity_y;")
    if want_ambient:
        p("extern float RenderSettings_ambient_r;")
        p("extern float RenderSettings_ambient_g;")
        p("extern float RenderSettings_ambient_b;")
    if plan.get("camera"):
        p("extern float Camera_main_pos_x;")
        p("extern float Camera_main_pos_y;")
        p("extern float Camera_main_orthographicSize;")
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
    if light_n:
        p("extern const int _Light_count;")
        p("extern float _Light_intensity[%d];" % light_n)
        p("extern float _Light_color_r[%d];" % light_n)
        p("extern float _Light_color_g[%d];" % light_n)
        p("extern float _Light_color_b[%d];" % light_n)
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
        p("extern %s _%s_inst_array[%d];" % (idn, idn, cl["n"]))
        p("extern const int _%s_inst_count;" % idn)
        if cl.get("soa_dims"):
            p("extern float _%s_pos[%d][%d];" % (idn, cl["n"], cl["soa_dims"]))
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

    if want_find or want_getcomponent:
        go_names = plan.get("go_names") or []
        go_comps = plan.get("go_components") or {}
        p("/* GameObject.Find / GetComponent — authored scene tables only */")
        p("static const int _engine_go_count = %d;" % len(go_names))
        if go_names:
            p("static const char *_engine_go_name[%d] = {" % len(go_names))
            for n in go_names:
                p("    %s," % _c_string(n))
            p("};")
        else:
            p("static const char *_engine_go_name[1] = { \"\" };")
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
            p("static const int _engine_go_%s[%d] = { %s };" % (
                idn, len(vals), ", ".join(vals)))
            # this instance i → GO index (for GetComponent on this).
            rev = ["-1"] * max(1, plan["classes"][cname]["n"])
            for n, cmap in go_comps.items():
                if cname in cmap and n in go_names:
                    gi = go_names.index(n)
                    rev[cmap[cname]] = str(gi)
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
        for cname in sorted(set(plan["classes"]) | getcomponent_types):
            if cname not in plan["classes"]:
                continue
            idn = _c_ident(cname)
            p("static int GameObject_GetComponent_%s(int go) {" % idn)
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_%s[go];" % idn)
            p("}")
            p("")

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

    p("void engine_tick(void) {")
    if "Time.time" in used_apis:
        p("    Time_time = Time_time + Time_deltaTime;")
    for cname in sorted(plan["classes"]):
        p("    %s_FixedTick();" % _c_ident(cname))
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
        p("        static const int _spr_tex[] = { %s };" % ", ".join(
            str(int(sp["tex_id"])) for _i, sp in spr_idx))
        p("        static const unsigned _spr_i[] = { %s };" % ", ".join(
            str(i) for i, _sp in spr_idx))
        p("        int k;")
        p("        for (k = 0; k < %d && n < max; k = k + 1) {" % len(spr_idx))
        p("            unsigned i = _spr_i[k];")
        p("            out[n].x = %s_get_pos_x(i);" % idn)
        p("            out[n].y = %s_get_pos_y(i);" % idn)
        p("            out[n].half_w = _spr_hw[k];")
        p("            out[n].half_h = _spr_hh[k];")
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
    text = re.sub(
        r"transform\.position\s*\+=\s*new\s+Vector2\s*\((.*?)\)\s*;",
        repl_add, text, flags=flags)
    text = re.sub(
        r"transform\.position\s*\+=\s*new\s+Vector3\s*\((.*?)\)\s*;",
        repl_add, text, flags=flags)
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


def _lower_method_body(body, cl, plan):
    """C# subset method → C against packed arrays.

    `this` / implicit fields become `_Class_inst_array[i].field`.
    `transform.position.x` becomes the packed pos_*. A class-typed
    field is already an index: `other.hp` → `_Other_inst_array[other].hp`.
    """
    idn = _c_ident(cl["name"])
    text = body
    text = re.sub(r"\bthis\.", "", text)
    # Find/GetComponent before field rewrites so `.amp` stays on the target type.
    text = _rewrite_find_getcomponent(text, plan, cl["name"])
    # API tokens before Vector2 rewrites so nested Mathf.Sin(...) keeps parens.
    text = text.replace("Time.deltaTime", "Time_deltaTime")
    text = text.replace("Time.fixedDeltaTime", "Time_fixedDeltaTime")
    text = text.replace("Time.time", "Time_time")
    text = text.replace("Physics2D.gravity.x", "Physics2D_gravity_x")
    text = text.replace("Physics2D.gravity.y", "Physics2D_gravity_y")
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
    # Unity Object.ToString when printing a Find result (name, not index).
    text = _wrap_log_gameobject_tostring(text)
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
    want_phys = "Physics2D.gravity" in used_apis
    want_input = bool(used_apis & _WANT_INPUT)
    want_keyboard = "Keyboard.current" in used_apis
    keyboard_keys = set(plan.get("keyboard_keys") or [])
    want_ambient = "RenderSettings.ambientLight" in used_apis
    lights = plan.get("lights") or []
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
    if "Time.fixedDeltaTime" in used_apis or want_phys:
        p("float Time_fixedDeltaTime = 0.02f;")
    if want_phys:
        p("float Physics2D_gravity_x = 0.f;")
        p("float Physics2D_gravity_y = -9.81f;")
    if want_ambient:
        # Unity default ambient-ish grey; host may override.
        p("float RenderSettings_ambient_r = 0.2f;")
        p("float RenderSettings_ambient_g = 0.2f;")
        p("float RenderSettings_ambient_b = 0.2f;")
    cam = plan.get("camera")
    if cam:
        p("float Camera_main_pos_x = %sf;" % repr(float(cam["pos"][0])))
        p("float Camera_main_pos_y = %sf;" % repr(float(cam["pos"][1])))
        p("float Camera_main_orthographicSize = %sf;" % repr(
            float(cam["orthographic_size"])))
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
    if lights:
        p("const int _Light_count = %d;" % len(lights))
        p("float _Light_intensity[%d] = { %s };" % (
            len(lights),
            ", ".join("%sf" % repr(float(L["intensity"])) for L in lights)))
        p("float _Light_color_r[%d] = { %s };" % (
            len(lights),
            ", ".join("%sf" % repr(float(L["r"])) for L in lights)))
        p("float _Light_color_g[%d] = { %s };" % (
            len(lights),
            ", ".join("%sf" % repr(float(L["g"])) for L in lights)))
        p("float _Light_color_b[%d] = { %s };" % (
            len(lights),
            ", ".join("%sf" % repr(float(L["b"])) for L in lights)))
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
        p("const int _%s_inst_count = %d;" % (idn, cl["n"]))
        if cl.get("soa_dims"):
            dims = cl["soa_dims"]
            logical = _soa_axis_count(cl)
            p("float _%s_pos[%d][%d] = {" % (idn, cl["n"], dims))
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
            p("};")
            p("")
        p("%s _%s_inst_array[%d] = {" % (idn, idn, cl["n"]))
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
    guids = _guid_map(root)
    assets = _asset_guid_map(root)
    objects = []
    lights = []
    cameras = []
    for path in _walk_files(root, (".unity",)):
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

    scripts = _walk_files(root, (".cs",))
    # Godot / Blender may name a class with no .cs; synthesise an empty one.
    analyses = [analyze_script(p) for p in scripts]
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
    if "Camera.main" in used_apis and not cameras:
        raise PackError(
            "Camera.main: no Camera in the scene — unity_pack does not invent "
            "a default camera. Add an authored Camera (tag MainCamera).")
    plan = plan_layouts(objects, analyses)
    if soa or soa_vec4:
        plan = apply_soa_layout(plan, vec4=bool(soa_vec4))
    else:
        plan = dict(plan)
        plan["soa"] = False
        plan["soa_vec4"] = False
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
    os.makedirs(outdir, exist_ok=True)
    engine = emit_engine(plan, analyses, used_apis)
    data = emit_data(plan, used_apis)
    main_c = emit_main()
    validate_emitted_c(engine, "engine.c")
    validate_emitted_c(data, "data.c")
    validate_emitted_c(main_c, "main.c")
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
