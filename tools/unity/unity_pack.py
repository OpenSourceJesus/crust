#!/usr/bin/env python3
"""unity_pack -- build a Unity-subset project with CCSharp.

Reads the project's build scenes (and the prefabs its scripts refer to), then
hands them to tools/unity/unity_pack_ccs.py, which writes the scene as C#,
copies the scripts and lowers the lot with CCSharp's `ccs2c.py
--unity-engine`.  A project the engine does not cover is refused.

    python3 tools/unity/unity_pack.py <project>
    python3 tools/unity/unity_pack.py <project> -o <outdir>

Default output is $TMPDIR/<project-folder>/: game (the executable), game.c,
and engine.c + engine_draw.h for the examples/unity_pack hosts.

    CCSHARP_HOME   the CCSharp checkout (default ../CCSharp beside this repo)
"""

from __future__ import annotations

import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tools.unity.unity_pack_curves as _curves  # noqa: E402
import tools.unity.unity_pack_lines as _lines  # noqa: E402
import tools.unity.unity_pack_mesh as _mesh  # noqa: E402
import tools.unity.unity_pack_particles as _parts  # noqa: E402
import tools.unity.unity_pack_tilemap as _tm  # noqa: E402
from tools.unity.unity_pack_anim import (  # noqa: E402
    _load_animation_assets)
from tools.unity.unity_pack_common import (  # noqa: E402
    PackError, _authored_camera_view_size, _class_name_from_cs, _fit_preserve_aspect, _is_unity_builtin_guid, _layout_child_sizes,
    _mark_hierarchy_live, _mb_enabled, _parse_pad_int, _progress, _read, _ui_local_rect_wh,
    _yaml_vec2)
from tools.unity.unity_pack_physics import (  # noqa: E402
    _DEFAULT_MAT2D, _DEFAULT_MAT3D, _JOINT2D_COMPONENTS, _JOINT2D_KINDS, _PHYSICS_COMPONENTS, _load_physics_materials,
    _parse_joint2d, _triangulate_paths, read_layer_matrix, read_layer_names)
from tools.unity.unity_pack_sprites import (  # noqa: E402
    _apply_sprite_sorting, _attach_sprite_textures, _layout_parent_pixel_size, _load_sprite_rgba)
from tools.unity.unity_pack_ui import (  # noqa: E402
    _UI_GETCOMPONENT_TYPES, _annotate_ui_button_onclick_targets, _annotate_ui_eventtrigger_targets, _annotate_ui_scrollbar_onvaluechanged_targets, _annotate_ui_slider_onvaluechanged_targets, _annotate_ui_toggle_onvaluechanged_targets,
    _apply_rect_property_mods, _apply_scrollbar_visuals, _apply_slider_visuals, _apply_ui_button_onclick_mods, _apply_ui_image_sprite_mod, _builtin_uisprite,
    _bump_ui_hierarchy_draw_order, _canvas_by_gameobject, _is_canvas_scaler_mb, _is_ui_button_mb, _is_ui_eventtrigger_mb, _is_ui_image_mb,
    _is_ui_scrollbar_mb, _is_ui_scrollrect_mb, _is_ui_slider_mb, _is_ui_tmp_mb, _is_ui_toggle_mb, _is_ui_togglegroup_mb,
    _layout_group_calc_along_axis, _load_tmp_font_asset, _parse_canvas_scaler, _parse_hv_layout_group, _parse_ui_button, _parse_ui_eventtrigger,
    _parse_ui_scrollbar, _parse_ui_scrollrect, _parse_ui_slider, _parse_ui_tmp, _parse_ui_toggle, _parse_ui_togglegroup,
    _prefab_mod_rect, _rasterize_tmp_text, _snapshot_ui_rects_onto_hierarchy, _ui_bake_pixel_scale, _ui_layout_screen, _ui_mark_canvas_units,
    _ui_screen_rect)


# Built-in Unity components AddComponent may create at runtime.
_ADDABLE_BUILTINS = frozenset((
    "Camera",
    "Light",
    "SpriteRenderer",
    "Rigidbody2D",
    "Rigidbody",
    "BoxCollider2D",
    "CircleCollider2D",
    "PolygonCollider2D",
    "BoxCollider",
    "SphereCollider",
    "Animation",
    "Animator",
    "AudioSource",
    "LineRenderer",     # spare rows (tools/unity/unity_pack_lines.py)
))


# Types that still require inventing assets / systems — AddComponent refused.
_REFUSED_ADDCOMPONENT = frozenset((
    "ParticleSystem",
    "Canvas",
))


# Every GameObject has a Transform (RectTransform is the uGUI subclass).
# GetComponent<Transform|RectTransform>() ≡ GO index (same as .transform).
_TRANSFORM_GETCOMPONENT_TYPES = frozenset(("Transform", "RectTransform"))


# ParticleSystem: read, simulated and drawn (tools/unity/unity_pack_particles.py).
_PARTICLE_COMPONENTS = frozenset(("ParticleSystem",))


#: a serialized number as Unity writes it (`1e-05`, `Infinity`, `NaN` too)
_YAML_NUM = r"-?(?:\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|Infinity)|NaN"


def _yaml_scalar(raw):
    """Strip a simple Unity YAML scalar (optional quotes)."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s


def _apply_camera_script_view_to_cameras(cameras, objects):
    """Match CameraScript.HandleViewSize orthographicSize on the main camera."""
    view = _authored_camera_view_size(objects)
    if view is None or not cameras:
        return
    vw, vh = view
    if vh < 1e-6:
        return
    aspect = vw / vh
    ortho = max(vw * 0.5 / aspect, vh * 0.5) if aspect > 1e-6 else vh * 0.5
    main = None
    for c in cameras:
        if c.get("main"):
            main = c
            break
    if main is None:
        main = cameras[0]
    main["orthographic_size"] = ortho


def _load_sorting_layers(root):
    """TagManager.asset m_SortingLayers → [{name, unique_id}, ...] in draw order.

    Earlier list entries are behind later ones (Unity painter's algorithm).
    Missing TagManager → single Default layer uniqueID 0.
    """
    path = os.path.join(root, "ProjectSettings", "TagManager.asset")
    layers = []
    if os.path.isfile(path):
        text = _read(path)
        sm = re.search(
            r"(?ms)^\s*m_SortingLayers:\s*\n(.*?)(?=^\s*m_[A-Za-z]|\Z)",
            text)
        if sm:
            for m in re.finditer(
                    r"(?ms)^\s*-\s*name:\s*(.*?)\n\s*uniqueID:\s*(\d+)",
                    sm.group(1)):
                name = _yaml_scalar(m.group(1)) or m.group(1).strip()
                layers.append({
                    "name": name,
                    "unique_id": int(m.group(2)),
                })
    if not layers:
        layers = [{"name": "Default", "unique_id": 0}]
    return layers


def _editor_build_settings_path(root):
    return os.path.join(root, "ProjectSettings", "EditorBuildSettings.asset")


def _parse_editor_build_scenes(root):
    """EditorBuildSettings ``m_Scenes`` entries: ``{enabled, path, guid}``.

    Missing file → empty list (caller falls back). Paths are project-relative
    Unity paths (``Assets/...``).
    """
    path = _editor_build_settings_path(root)
    if not os.path.isfile(path):
        return []
    try:
        text = _read(path)
    except IOError:
        return []
    out = []
    for m in re.finditer(
            r"(?m)^  - enabled:\s*(\d+)\s*\n"
            r"    path:\s*(.+?)\s*\n"
            r"    guid:\s*([0-9a-fA-F]+)\s*$",
            text):
        rel = m.group(2).strip().strip("'\"")
        out.append({
            "enabled": int(m.group(1)) != 0,
            "path": rel.replace("\\", "/"),
            "guid": m.group(3).lower(),
        })
    return out


def _resolve_build_scene_path(root, entry, asset_guids=None):
    """Absolute path for a build-settings scene entry, or None if missing."""
    rel = (entry.get("path") or "").replace("\\", "/")
    if not rel:
        return None
    cand = os.path.join(root, rel)
    if os.path.isfile(cand):
        return os.path.abspath(cand)
    g = (entry.get("guid") or "").lower()
    if g and asset_guids:
        p = asset_guids.get(g)
        if p and str(p).lower().endswith(".unity") and os.path.isfile(p):
            return os.path.abspath(p)
    return None


def _unity_scenes_to_pack(root, asset_guids=None):
    """``.unity`` paths to pack, in build order: every enabled
    EditorBuildSettings scene. A scene's position here is its
    ``buildIndex``; the first is the one the player starts in.

    Scenes not listed in build settings are never packed. Disabled build
    entries are skipped (they have no build index). When
    ``EditorBuildSettings.asset`` is absent (tests / tiny fixtures), fall
    back to every ``.unity`` under ``Assets/`` in path order — never a
    whole-project walk that pulls vendor demo scenes.
    """
    root = os.path.abspath(root)
    entries = _parse_editor_build_scenes(root)
    if entries:
        enabled = [e for e in entries if e.get("enabled")]
        out = []
        for e in enabled:
            path = _resolve_build_scene_path(root, e, asset_guids=asset_guids)
            if not path:
                raise PackError(
                    "EditorBuildSettings: enabled scene %s not found"
                    % (e.get("path") or e.get("guid") or "?"))
            out.append(path)
        if out:
            return out
        raise PackError(
            "EditorBuildSettings: no enabled scenes "
            "(add a scene or enable one in File → Build Settings)")
    assets = os.path.join(root, "Assets")
    if os.path.isdir(assets):
        return sorted(_walk_files(assets, (".unity",)))
    return sorted(_walk_files(root, (".unity",)))


#: fileIDs are unique only within one scene file. Scenes after the first get
#: theirs XORed with ``index << _SCENE_FILE_ID_SHIFT`` so merged scene graphs
#: cannot alias; references carrying a ``guid`` point into other assets and
#: keep their IDs.
_SCENE_FILE_ID_SHIFT = 56


_LOCAL_FILE_ID_RE = re.compile(
    r"(^--- !u!\d+ &(?=-?\d+\s)|\{fileID: (?=-?\d+\}))(-?\d+)", re.M)


def _scene_local_file_ids(text, scene_index):
    """*text* with its scene-local fileIDs made unique to *scene_index*."""
    if not scene_index:
        return text
    tag = scene_index << _SCENE_FILE_ID_SHIFT

    def _sub(m):
        fid = int(m.group(2))
        return m.group(1) + str(fid ^ tag) if fid else m.group(0)
    return _LOCAL_FILE_ID_RE.sub(_sub, text)


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


def _walk_package_metas(root):
    """`.meta` under Packages/ and Library/PackageCache (UPM assets)."""
    out = []
    for rel in ("Packages", os.path.join("Library", "PackageCache")):
        base = os.path.join(root, rel)
        if not os.path.isdir(base):
            continue
        n_dirs = 0
        for dirpath, dirnames, names in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__")]
            n_dirs += 1
            if n_dirs % 500 == 0:
                _progress("  package walk %d dir(s), %d meta(s)"
                           % (n_dirs, len(out)))
            for n in names:
                if n.endswith(".meta"):
                    out.append(os.path.join(dirpath, n))
    out.sort()
    return out


def _path_under_assets(root, path):
    """True if *path* is under the project's Assets/ folder."""
    assets = os.path.join(os.path.abspath(root), "Assets")
    ap = os.path.abspath(path)
    return ap == assets or ap.startswith(assets + os.sep)


def _guid_map(root, asset_guids=None):
    """Unity .meta `guid:` next to a .cs file → script path.

    If *asset_guids* is provided (full guid→path map), derive script guids
    from it without a second tree walk. Only Assets/ scripts count — package
    scripts resolve for asset refs but must not become packed MonoBehaviours.
    """
    out = {}
    if asset_guids is not None:
        for g, path in asset_guids.items():
            if path.lower().endswith(".cs") and _path_under_assets(root, path):
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
    """Any Unity .meta guid → asset path (Assets, Packages, PackageCache)."""
    out = {}
    _progress("walking project tree for .meta files")
    metas = list(_walk_files(root, (".meta",)))
    _progress("indexing package .meta files (Packages / PackageCache)")
    metas.extend(_walk_package_metas(root))
    metas = sorted(set(metas))
    _progress("indexing %d .meta file(s)" % len(metas))
    for i, meta in enumerate(metas):
        if metas and ((i + 1) % 200 == 0 or i + 1 == len(metas)):
            _progress("  asset metas %d/%d" % (i + 1, len(metas)))
        text = _read(meta)
        m = re.search(r"(?m)^guid:\s*([0-9a-fA-F]+)\s*$", text)
        if not m:
            continue
        asset = meta[:-5] if meta.endswith(".meta") else meta
        g = m.group(1).lower()
        # Prefer Assets/ over PackageCache when the same guid appears twice.
        if g in out and _path_under_assets(root, out[g]):
            continue
        if g in out and not _path_under_assets(root, asset):
            continue
        out[g] = asset
    return out


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


def _quat_xy_basis(qx, qy, qz, qw):
    """Local XY → world XY after *rot* (orthographic drop of Z).

    Sprite quads live in the local XY plane; hosts apply
    ``(m00,m01; m10,m11) * (lx, ly)``. Pure Z spin matches cos/sin; X/Y
    tilt foreshortens the projected extents (Unity ortho SpriteRenderer).
    """
    m00 = 1.0 - 2.0 * (qy * qy + qz * qz)
    m01 = 2.0 * (qx * qy - qz * qw)
    m10 = 2.0 * (qx * qy + qz * qw)
    m11 = 1.0 - 2.0 * (qx * qx + qz * qz)
    return m00, m01, m10, m11


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


# uGUI layout controllers (authored Vertical/HorizontalLayoutGroup).
_VLAYOUT_SCRIPT_GUID = "59f8146938fff824cb5fd77236b75775"


_HLAYOUT_SCRIPT_GUID = "30649d3a9faa99c48a7b1166b86bf2a0"


_LAYOUT_ELEMENT_GUID = "306cc8c2b49d7114eaa3623786fc2126"


_CONTENT_SIZE_FITTER_GUID = "3245ec927659c4140ac4f8d17403cc18"


_ASPECT_RATIO_FITTER_GUID = "86710e43de46f6f4bac7c8e50813a599"


def _is_vlayout_mb(block, guid):
    g = (guid or "").lower()
    if g == _VLAYOUT_SCRIPT_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bVerticalLayoutGroup\s*$", block))


def _is_hlayout_mb(block, guid):
    g = (guid or "").lower()
    if g == _HLAYOUT_SCRIPT_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bHorizontalLayoutGroup\s*$",
        block))


def _is_layout_element_mb(block, guid):
    g = (guid or "").lower()
    if g == _LAYOUT_ELEMENT_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bLayoutElement\s*$", block))


def _is_content_size_fitter_mb(block, guid):
    g = (guid or "").lower()
    if g == _CONTENT_SIZE_FITTER_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bContentSizeFitter\s*$", block))


def _is_aspect_ratio_fitter_mb(block, guid):
    g = (guid or "").lower()
    if g == _ASPECT_RATIO_FITTER_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bAspectRatioFitter\s*$", block))


def _parse_layout_element(block):
    def _f(key):
        m = re.search(
            r"(?m)^\s+%s:\s*([0-9.eE+-]+)" % re.escape(key), block)
        return float(m.group(1)) if m else -1.0
    return {
        "enabled": _mb_enabled(block),
        "ignore": _parse_pad_int(block, "m_IgnoreLayout", 0),
        "min": (_f("m_MinWidth"), _f("m_MinHeight")),
        "preferred": (_f("m_PreferredWidth"), _f("m_PreferredHeight")),
        "flexible": (_f("m_FlexibleWidth"), _f("m_FlexibleHeight")),
        "max": (_f("m_MaxWidth"), _f("m_MaxHeight")),
        "priority": _parse_pad_int(block, "m_LayoutPriority", 1),
    }


def _parse_content_size_fitter(block):
    """Authored ContentSizeFitter — FitMode per axis (0 Unconstrained … 3 Clamped)."""
    return {
        "enabled": _mb_enabled(block),
        "horizontal": _parse_pad_int(block, "m_HorizontalFit", 0),
        "vertical": _parse_pad_int(block, "m_VerticalFit", 0),
    }


def _parse_aspect_ratio_fitter(block):
    """Authored AspectRatioFitter — AspectMode + width/height ratio."""
    ar = re.search(r"(?m)^\s+m_AspectRatio:\s*([0-9.eE+-]+)", block)
    ratio = float(ar.group(1)) if ar else 1.0
    if ratio < 0.001:
        ratio = 0.001
    if ratio > 1000.0:
        ratio = 1000.0
    return {
        "enabled": _mb_enabled(block),
        "mode": _parse_pad_int(block, "m_AspectMode", 0),
        "ratio": ratio,
    }


def _layout_alignment_on_axis(child_alignment, axis):
    """TextAnchor → 0 left/top, 0.5 middle, 1 right/bottom (Unity LayoutGroup)."""
    a = int(child_alignment)
    if axis == 0:
        return (a % 3) * 0.5
    return (a // 3) * 0.5


def _layout_set_size_with_current_anchors(obj, axis, size, parent_size):
    """Unity RectTransform.SetSizeWithCurrentAnchors — sizeDelta from target size."""
    rect = obj.get("rect")
    if not rect:
        return
    amin = rect.get("anchor_min") or (0.5, 0.5)
    amax = rect.get("anchor_max") or (0.5, 0.5)
    sd = list(rect.get("size_delta") or (0.0, 0.0))
    span = float(amax[axis]) - float(amin[axis])
    sd[axis] = float(size) - float(parent_size[axis]) * span
    rect["size_delta"] = (sd[0], sd[1])


def _layout_le_axis(le, axis, cur):
    """LayoutElement → (min, preferred, max, flexible); negatives → defaults."""
    mn = float((le.get("min") or (-1.0, -1.0))[axis])
    pref = float((le.get("preferred") or (-1.0, -1.0))[axis])
    flex = float((le.get("flexible") or (-1.0, -1.0))[axis])
    mx = float((le.get("max") or (-1.0, -1.0))[axis])
    if mn < 0.0:
        mn = 0.0
    if pref < 0.0:
        pref = cur
    if flex < 0.0:
        flex = 0.0
    if mx < 0.0:
        mx = float("inf")
    if pref > mx:
        pref = mx
    if mn > mx:
        mn = mx
    return mn, pref, mx, flex


def _layout_query_sizes(obj, axis, children_map):
    """Aggregate ILayoutElement sizes (LayoutElement + LayoutGroup) for one axis.

    Higher ``layoutPriority`` wins; same priority takes the max of each field
    (Unity LayoutUtility). LayoutGroup priority is 0; LayoutElement default 1.
    """
    cur = abs(float(((obj.get("rect") or {}).get("size_delta") or (0.0, 0.0))[axis]))
    entries = []  # (priority, min, pref, max, flex)
    le = obj.get("layout_element")
    if le and not le.get("ignore") and int(le.get("enabled", 1)):
        mn, pref, mx, flex = _layout_le_axis(le, axis, cur)
        entries.append((int(le.get("priority") or 1), mn, pref, mx, flex))
    lg = obj.get("layout_group")
    if lg and int(lg.get("enabled", 1)):
        kids = [c for c in children_map.get(str(obj.get("xf_id") or ""), [])
                if c.get("rect") is not None]
        mn, pref, mx, flex = _layout_group_calc_along_axis(obj, kids, axis)
        entries.append((0, mn, pref, mx, flex))
    if not entries:
        return cur, cur, float("inf"), 0.0
    best_p = max(e[0] for e in entries)
    top = [e for e in entries if e[0] == best_p]
    mn = max(e[1] for e in top)
    pref = max(e[2] for e in top)
    # Max: LayoutUtility uses the *minimum* positive max among same-priority
    # sources when comparing (GetMaxLayoutProperty). Prefer finite mins.
    maxes = [e[3] for e in top if e[3] < float("inf")]
    mx = min(maxes) if maxes else float("inf")
    flex = max(e[4] for e in top)
    if pref > mx:
        pref = mx
    if mn > mx:
        mn = mx
    if pref < mn:
        pref = mn
    return mn, pref, mx, flex


def _apply_content_size_fitter(obj, children_map, by_xf, screen_w, screen_h):
    """Bake ContentSizeFitter into sizeDelta (Unity HandleSelfFittingAlongAxis)."""
    csf = obj.get("content_size_fitter") or {}
    parent_size = _layout_parent_pixel_size(obj, by_xf, screen_w, screen_h)
    cache = {}
    cur_w, cur_h = _ui_local_rect_wh(
        obj, by_xf, screen_w, screen_h, cache)
    cur = (abs(cur_w), abs(cur_h))
    for axis, fit_key in ((0, "horizontal"), (1, "vertical")):
        fit = int(csf.get(fit_key) or 0)
        if fit == 0:  # Unconstrained
            continue
        mn, pref, mx, _flex = _layout_query_sizes(obj, axis, children_map)
        if fit == 1:  # MinSize
            size = mn
        elif fit == 2:  # PreferredSize
            size = pref
        elif fit == 3:  # Clamped
            size = cur[axis]
            if size < mn:
                size = mn
            if size > mx:
                size = mx
        else:
            continue
        _layout_set_size_with_current_anchors(obj, axis, size, parent_size)


def _apply_aspect_ratio_fitter(obj, by_xf, screen_w, screen_h):
    """Bake AspectRatioFitter into anchors / sizeDelta (Unity UpdateRect)."""
    arf = obj.get("aspect_ratio_fitter") or {}
    mode = int(arf.get("mode") or 0)
    if mode == 0:
        return
    ratio = float(arf.get("ratio") or 1.0)
    if ratio < 0.001:
        ratio = 0.001
    rect = obj.get("rect")
    if not rect:
        return
    parent_size = _layout_parent_pixel_size(obj, by_xf, screen_w, screen_h)
    cache = {}
    cur_w, cur_h = _ui_local_rect_wh(
        obj, by_xf, screen_w, screen_h, cache)
    cur_w, cur_h = abs(cur_w), abs(cur_h)
    if mode == 2:  # HeightControlsWidth
        _layout_set_size_with_current_anchors(
            obj, 0, cur_h * ratio, parent_size)
    elif mode == 1:  # WidthControlsHeight
        _layout_set_size_with_current_anchors(
            obj, 1, cur_w / ratio, parent_size)
    elif mode in (3, 4):  # FitInParent / EnvelopeParent
        if obj.get("father_id") is None:
            return
        rect["anchor_min"] = (0.0, 0.0)
        rect["anchor_max"] = (1.0, 1.0)
        rect["anchored_position"] = (0.0, 0.0)
        pw, ph = parent_size
        # sizeDelta to produce size: size - parent * (amax-amin) = size - parent
        # when anchors are 0..1.
        fit = (mode == 3)
        # (parent.y * ratio < parent.x) XOR FitInParent
        if (ph * ratio < pw) ^ fit:
            # Drive height from parent width / ratio
            target_h = pw / ratio
            rect["size_delta"] = (0.0, target_h - ph)
        else:
            target_w = ph * ratio
            rect["size_delta"] = (target_w - pw, 0.0)


def _layout_set_child_axis(child, axis, pos, size, scale, control):
    """Mirror Unity LayoutGroup.SetChildAlongAxisWithScale (anchors → top-left)."""
    rect = child.get("rect")
    if not rect:
        return
    amin = list(rect.get("anchor_min") or (0.5, 0.5))
    amax = list(rect.get("anchor_max") or (0.5, 0.5))
    # Vector2.up — top-left driven anchors.
    amin[0], amin[1] = 0.0, 1.0
    amax[0], amax[1] = 0.0, 1.0
    sd = list(rect.get("size_delta") or (0.0, 0.0))
    apos = list(rect.get("anchored_position") or (0.0, 0.0))
    pivot = rect.get("pivot") or (0.5, 0.5)
    sc = float(scale) if scale else 1.0
    if control:
        sd[axis] = float(size)
        use_size = float(size)
    else:
        use_size = abs(float(sd[axis]))
    if axis == 0:
        apos[0] = float(pos) + use_size * float(pivot[0]) * sc
    else:
        apos[1] = -float(pos) - use_size * (1.0 - float(pivot[1])) * sc
    rect["anchor_min"] = (amin[0], amin[1])
    rect["anchor_max"] = (amax[0], amax[1])
    rect["size_delta"] = (sd[0], sd[1])
    rect["anchored_position"] = (apos[0], apos[1])


def _layout_set_children_along_axis(parent, children, axis, is_vertical,
                                     parent_size):
    """Unity HorizontalOrVerticalLayoutGroup.SetChildrenAlongAxis (authored)."""
    lg = parent.get("layout_group") or {}
    control = bool(lg.get(
        "child_control_width" if axis == 0 else "child_control_height", 1))
    force = bool(lg.get(
        "child_force_expand_width" if axis == 0
        else "child_force_expand_height", 1))
    use_scale = bool(lg.get(
        "child_scale_width" if axis == 0 else "child_scale_height", 0))
    spacing = float(lg.get("spacing") or 0.0)
    pad_l = float(lg.get("pad_left") or 0.0)
    pad_r = float(lg.get("pad_right") or 0.0)
    pad_t = float(lg.get("pad_top") or 0.0)
    pad_b = float(lg.get("pad_bottom") or 0.0)
    pad_cross = (pad_l + pad_r) if axis == 0 else (pad_t + pad_b)
    align = _layout_alignment_on_axis(lg.get("child_alignment") or 0, axis)
    along_other = bool(is_vertical) ^ (axis == 1)
    size = float(parent_size[axis])
    kids = list(children)
    if lg.get("reverse"):
        kids = list(reversed(kids))

    sizes = []
    for ch in kids:
        sc = _layout_child_sizes(ch, axis, control, force)
        if sc is None:
            sizes.append(None)
            continue
        mn, pref, flex = sc
        scale = 1.0
        if use_scale:
            ls = ch.get("local_scale") or (1.0, 1.0, 1.0)
            scale = abs(float(ls[axis]))
            if scale < 1e-8:
                scale = 1.0
        sizes.append((mn, pref, flex, scale))

    if along_other:
        inner = size - pad_cross
        for i, ch in enumerate(kids):
            if sizes[i] is None:
                continue
            mn, pref, flex, scale = sizes[i]
            required = max(mn, min(inner, pref if flex <= 0 else size))
            start = ((pad_l if axis == 0 else pad_t)
                     + (inner - required * scale) * align)
            if control:
                _layout_set_child_axis(ch, axis, start, required, scale, True)
            else:
                sd = abs(float(((ch.get("rect") or {}).get(
                    "size_delta") or (0, 0))[axis]))
                offset = (required - sd) * align
                _layout_set_child_axis(
                    ch, axis, start + offset, sd, scale, False)
        return

    # Primary axis: stack with spacing + flexible surplus.
    total_min = pad_cross
    total_pref = pad_cross
    total_flex = 0.0
    n_count = 0
    for sc in sizes:
        if sc is None:
            continue
        mn, pref, flex, scale = sc
        total_min += mn * scale + spacing
        total_pref += pref * scale + spacing
        total_flex += flex
        n_count += 1
    if n_count > 0:
        total_min -= spacing
        total_pref -= spacing
    pos = pad_l if axis == 0 else pad_t
    surplus = size - total_pref
    item_flex_mul = 0.0
    if surplus > 0.0:
        if total_flex <= 0.0:
            # No flexible: align the block as a whole.
            needed = total_pref - pad_cross
            pos = ((pad_l if axis == 0 else pad_t)
                   + (size - pad_cross - needed) * align)
        else:
            item_flex_mul = surplus / total_flex
    min_max_lerp = 0.0
    if abs(total_pref - total_min) > 1e-6:
        min_max_lerp = max(0.0, min(1.0,
            (size - total_min) / (total_pref - total_min)))
    for i, ch in enumerate(kids):
        if sizes[i] is None:
            continue
        mn, pref, flex, scale = sizes[i]
        child_size = mn + (pref - mn) * min_max_lerp
        child_size = child_size + flex * item_flex_mul
        if control:
            _layout_set_child_axis(ch, axis, pos, child_size, scale, True)
        else:
            sd = abs(float(((ch.get("rect") or {}).get(
                "size_delta") or (0, 0))[axis]))
            offset = (child_size - sd) * align
            _layout_set_child_axis(
                ch, axis, pos + offset, sd, scale, False)
        pos = pos + child_size * scale + spacing


def _apply_layout_groups(objects, screen_w, screen_h):
    """Bake uGUI layout controllers into RectTransforms.

    Order mirrors Unity LayoutRebuilder for authored-only cases:
    1. ContentSizeFitter (deepest first) — preferred/min from LayoutElement
       and Vertical/HorizontalLayoutGroup child totals.
    2. Vertical/HorizontalLayoutGroup (shallow first) — child positions/sizes.
    3. AspectRatioFitter (shallow first) — aspect against parent size.

    Mutates ``rect`` so ``_ui_screen_rect`` / ``_bake_ui_images`` see Unity's
    laid-out positions.
    """
    by_xf = {}
    children = {}
    for o in objects:
        xid = o.get("xf_id")
        if not xid:
            continue
        by_xf[str(xid)] = o
        fid = o.get("father_id")
        if fid:
            children.setdefault(str(fid), []).append(o)

    depth = {}

    def _depth(o, guard=0):
        xid = str(o.get("xf_id") or "")
        if xid in depth:
            return depth[xid]
        if guard > 64 or o.get("canvas"):
            depth[xid] = 0
            return 0
        fid = o.get("father_id")
        parent = by_xf.get(str(fid)) if fid else None
        if parent is None:
            depth[xid] = 0
            return 0
        d = _depth(parent, guard + 1) + 1
        depth[xid] = d
        return d

    fitters = [o for o in objects
               if o.get("content_size_fitter") and o.get("rect") and o.get("xf_id")
               and int(o["content_size_fitter"].get("enabled", 1))]
    fitters.sort(key=lambda o: -_depth(o))
    for o in fitters:
        _apply_content_size_fitter(o, children, by_xf, screen_w, screen_h)

    groups = [o for o in objects
              if o.get("layout_group") and o.get("xf_id")
              and int(o["layout_group"].get("enabled", 1))]
    groups.sort(key=lambda o: _depth(o))
    for parent in groups:
        kids = [c for c in children.get(str(parent["xf_id"]), [])
                if c.get("rect") is not None]
        if not kids:
            continue
        # Honor authored m_Children order (not objects[] discovery order).
        order = {str(cid): i
                 for i, cid in enumerate(parent.get("child_ids") or [])}
        kids.sort(key=lambda c: order.get(str(c.get("xf_id")), 10 ** 9))
        # LayoutGroup uses parent.rect (pre-localScale); screen mapping
        # applies ancestor scales in _ui_screen_rect.
        cache = {}
        pw, ph = _ui_local_rect_wh(
            parent, by_xf, screen_w, screen_h, cache)
        parent_size = (abs(pw), abs(ph))
        is_vert = bool(parent["layout_group"].get("vertical"))
        _layout_set_children_along_axis(
            parent, kids, 0, is_vert, parent_size)
        _layout_set_children_along_axis(
            parent, kids, 1, is_vert, parent_size)

    arfs = [o for o in objects
            if o.get("aspect_ratio_fitter") and o.get("rect") and o.get("xf_id")
            and int(o["aspect_ratio_fitter"].get("enabled", 1))]
    arfs.sort(key=lambda o: _depth(o))
    for o in arfs:
        _apply_aspect_ratio_fitter(o, by_xf, screen_w, screen_h)


def _sample_rgba(tex, tw, th, u, v):
    """Bilinear sample RGBA texture (y=0 bottom); u/v in [0,1]."""
    if tw < 1 or th < 1:
        return (0, 0, 0, 0)
    x = max(0.0, min(float(tw) - 1.0, u * (tw - 1)))
    y = max(0.0, min(float(th) - 1.0, v * (th - 1)))
    x0 = int(math.floor(x))
    y0 = int(math.floor(y))
    x1 = min(x0 + 1, tw - 1)
    y1 = min(y0 + 1, th - 1)
    fx = x - x0
    fy = y - y0

    def _px(ix, iy):
        o = (iy * tw + ix) * 4
        return (tex[o], tex[o + 1], tex[o + 2], tex[o + 3])

    c00 = _px(x0, y0)
    c10 = _px(x1, y0)
    c01 = _px(x0, y1)
    c11 = _px(x1, y1)
    out = []
    for i in range(4):
        top = c00[i] * (1 - fx) + c10[i] * fx
        bot = c01[i] * (1 - fx) + c11[i] * fx
        out.append(int(round(top * (1 - fy) + bot * fy)))
    return tuple(out)


def _nine_slice_map(pos, size, border0, border1, src0, src1, src_size):
    """Map destination pixel coordinate → source [0,1] along one axis.

    border0/border1 are dest border sizes; src0/src1 are source border pixels.
    """
    if size <= 1e-6:
        return 0.5
    # Corners: fixed; edges/center: stretch.
    if pos < border0 and border0 > 1e-6 and src0 > 0:
        return (pos / border0) * (src0 / float(src_size))
    if pos >= size - border1 and border1 > 1e-6 and src1 > 0:
        t = (pos - (size - border1)) / border1
        return ((src_size - src1) + t * src1) / float(src_size)
    # Middle
    mid_dst = size - border0 - border1
    mid_src = src_size - src0 - src1
    if mid_dst <= 1e-6 or mid_src <= 0:
        return (src0 + mid_src * 0.5) / float(src_size)
    t = (pos - border0) / mid_dst
    return (src0 + t * mid_src) / float(src_size)


def _bake_sliced_rgba(src, sw, sh, border, dst_w, dst_h, ppu_mul=1.0):
    """9-slice bake source sprite into dst_w×dst_h (y=0 bottom).

    border is (left, bottom, right, top) in source pixels. ppu_mul is
    Image.m_PixelsPerUnitMultiplier (Unity shrinks borders when > 1).
    """
    dw = max(1, int(round(float(dst_w))))
    dh = max(1, int(round(float(dst_h))))
    mul = float(ppu_mul) if ppu_mul and float(ppu_mul) > 1e-6 else 1.0
    bl = max(0.0, float(border[0]) / mul)
    bb = max(0.0, float(border[1]) / mul)
    br = max(0.0, float(border[2]) / mul)
    bt = max(0.0, float(border[3]) / mul)
    # Dest borders clamp so corners never exceed half the rect.
    dbl = min(bl, dw * 0.5)
    dbr = min(br, dw * 0.5)
    dbb = min(bb, dh * 0.5)
    dbt = min(bt, dh * 0.5)
    if dbl + dbr > dw:
        s = dw / (dbl + dbr) if (dbl + dbr) > 0 else 0.0
        dbl *= s
        dbr *= s
    if dbb + dbt > dh:
        s = dh / (dbb + dbt) if (dbb + dbt) > 0 else 0.0
        dbb *= s
        dbt *= s
    out = bytearray(dw * dh * 4)
    for y in range(dh):
        # y is bottom-first; map with bottom border first.
        v = _nine_slice_map(y + 0.5, dh, dbb, dbt, bb, bt, sh)
        for x in range(dw):
            u = _nine_slice_map(x + 0.5, dw, dbl, dbr, bl, br, sw)
            r, g, b, a = _sample_rgba(src, sw, sh, u, v)
            o = (y * dw + x) * 4
            out[o] = r
            out[o + 1] = g
            out[o + 2] = b
            out[o + 3] = a
    return dw, dh, bytes(out)


def _bake_stretched_rgba(src, sw, sh, dst_w, dst_h):
    """Stretch source into dst (Simple Image.type). Never past the source's
    own size: the quad is drawn at the rect's size whatever the texture's,
    and a rect tens of thousands of pixels wide (Slime Jump's Background)
    baked billions of pixels in this loop."""
    dw = max(1, min(int(round(float(dst_w))), int(sw)))
    dh = max(1, min(int(round(float(dst_h))), int(sh)))
    out = bytearray(dw * dh * 4)
    for y in range(dh):
        v = (y + 0.5) / float(dh)
        for x in range(dw):
            u = (x + 0.5) / float(dw)
            r, g, b, a = _sample_rgba(src, sw, sh, u, v)
            o = (y * dw + x) * 4
            out[o] = r
            out[o + 1] = g
            out[o + 2] = b
            out[o + 3] = a
    return dw, dh, bytes(out)


def _preserve_aspect_draw_center(cx, cy, rw, rh, fw, fh, pivot):
    """Center of preserveAspect draw quad (Unity PreserveSpriteAspectRatio).

    Unity shrinks one axis then offsets that axis by
    ``(old - new) * pivot`` so a top-left pivot keeps the fitted sprite
    flush with the RectTransform top-left (not letterboxed to mid).
    """
    px = float((pivot or (0.5, 0.5))[0])
    py = float((pivot or (0.5, 0.5))[1])
    return (
        float(cx) + (float(rw) - float(fw)) * (px - 0.5),
        float(cy) + (float(rh) - float(fh)) * (py - 0.5),
    )


def _bake_tmp(tmp, text, font=None):
    """_rasterize_tmp_text of *text* in the TMP *tmp*'s rect (its "bake",
    set by _bake_ui_images): ``(w, h, rgba, shift_x, shift_y)`` pixels."""
    b = tmp["bake"]
    return _rasterize_tmp_text(
        font or _load_tmp_font_asset(b["font"]), text, b["size"],
        (1.0, 1.0, 1.0, 1.0),  # color via sprite tint (m_fontColor)
        b["box_w"], b["box_h"],
        int(tmp.get("h_align") or 1),
        int(tmp.get("v_align") or 256),
        int(tmp.get("overflow_mode") or 0),
        wrap=bool(tmp.get("wrap")), auto_size=b.get("auto_size"),
        line_spacing=float(tmp.get("line_spacing") or 0.0))


def _bake_ui_images(objects, cameras, screen_w, screen_h, asset_guids=None,
                    hierarchy=None, pixel_scale=1.0):
    """Resolve authored uGUI Image / TextMeshProUGUI → world sprites.

    Screen Space Overlay (0) and Screen Space Camera (1): map canvas pixels to
    the main ortho camera frustum. Nested World Space (2) canvases under a
    Screen Space ancestor still bake (use ancestor mode for mapping, nested
    Override Sorting for draw order). Standalone World Space is mapped as
    Overlay so Images are not dropped. Project PNG sprites and Unity builtin
    UISprites draw; Image.type Sliced 9-slices with sprite borders (UISprite
    corners stay fixed). Authored ``m_PreserveAspect`` on Simple Images fits
    the sprite inside the RectTransform (Unity GenerateSimpleSprite) instead
    of stretching.
    Empty m_Sprite is skipped (no invent). TMP needs an authored font asset
    with atlas + glyph tables.

    ``hierarchy`` supplies father links for stripped PrefabInstance transforms
    so Canvas sorting walks past button roots that are not packed objects.
    ``pixel_scale`` (screen pixels per canvas unit, <= 1) sets the texture
    resolution; draw sizes stay in canvas units.
    """
    k = min(1.0, max(1e-3, float(pixel_scale)))
    by_xf = {}
    for o in objects:
        xid = o.get("xf_id")
        if xid:
            by_xf[str(xid)] = o
    hier_father = {}
    for h in hierarchy or []:
        xid = h.get("xf_id")
        if xid is None:
            continue
        hier_father[str(xid)] = h.get("father_id")
    main = None
    for c in cameras or []:
        if c.get("main"):
            main = c
            break
    if main is None and cameras:
        main = cameras[0]
    cam_x = float((main or {}).get("pos", (0, 0, 0))[0])
    cam_y = float((main or {}).get("pos", (0, 0, 0))[1])
    ortho = float((main or {}).get("orthographic_size") or 5.0)
    if ortho < 1e-6:
        ortho = 5.0
    sw = max(1, int(screen_w))
    sh = max(1, int(screen_h))
    aspect = float(sw) / float(sh)
    world_h = 2.0 * ortho
    world_w = world_h * aspect
    px_w = world_w / float(sw)
    px_h = world_h / float(sh)
    rect_cache = {}
    asset_guids = asset_guids or {}
    png_cache = {}

    def _find_canvas(o):
        """Resolve Canvas for bake: sorting from nested override, mode from SS.

        PrefabInstance UI buttons often add a nested Canvas (World Space +
        Override Sorting) on the button root. Sorting must come from that
        nested canvas so Images draw above sibling menus; bake mapping still
        needs Overlay / Screen Space Camera from an ancestor (nested World
        Space alone would skip the draw).
        """
        default = {"render_mode": 0, "sorting_layer_id": 0,
                   "sorting_order": 0, "enabled": 1, "override_sorting": 0}
        # Nearest-first walk: self, then parents (packed or hierarchy stubs).
        nodes = []
        cur = o
        guard = 0
        seen = set()
        while cur is not None and guard < 64:
            guard += 1
            xid = str(cur.get("xf_id") or id(cur))
            if xid in seen:
                break
            seen.add(xid)
            nodes.append(cur)
            fid = cur.get("father_id")
            if not fid:
                break
            fid_s = str(fid)
            parent = by_xf.get(fid_s)
            if parent is not None:
                cur = parent
                continue
            # Climb hierarchy-only fathers until a packed object appears.
            hops = 0
            while fid_s and hops < 64 and fid_s not in by_xf:
                hops += 1
                if fid_s not in hier_father:
                    fid_s = None
                    break
                nxt = hier_father[fid_s]
                fid_s = str(nxt) if nxt is not None else None
            cur = by_xf.get(fid_s) if fid_s else None
        sort_c = None
        first_c = None
        root_c = None
        mode_c = None
        for node in nodes:
            c = node.get("canvas")
            if not c:
                continue
            if first_c is None:
                first_c = c
            root_c = c
            if sort_c is None and int(c.get("override_sorting") or 0):
                sort_c = c
            if (mode_c is None
                    and int(c.get("render_mode", 0)) in (0, 1)):
                mode_c = c
        # No Override Sorting → inherit root canvas order (Unity default).
        if sort_c is None:
            sort_c = root_c or first_c
        if sort_c is None:
            return default
        out = dict(sort_c)
        if mode_c is not None:
            out["render_mode"] = int(mode_c.get("render_mode", 0))
            out["camera_file_id"] = int(mode_c.get("camera_file_id") or 0)
            # Unity: Screen Space Camera with no camera renders as Overlay
            if out["render_mode"] == 1 and not out["camera_file_id"]:
                out["render_mode"] = 0
        elif int(out.get("render_mode", 0)) not in (0, 1):
            # Nested World Space with no SS ancestor: still map as Overlay.
            out["render_mode"] = 0
        return out

    def _apply_layout(o, cx, cy, rw, rh, canvas, source, color, extra=None):
        """Map rect to UI hit + sprite (rw/rh are draw size)."""
        hit_rw = abs(float(rw))
        hit_rh = abs(float(rh))
        wx = cam_x + (cx / float(sw) - 0.5) * world_w
        wy = cam_y + (cy / float(sh) - 0.5) * world_h
        o["pos"] = (wx, wy, float(o["pos"][2]) if o.get("pos") else 0.0)
        o["ui_hit"] = {
            "cx": float(cx), "cy": float(cy),
            "hw": hit_rw * 0.5, "hh": hit_rh * 0.5,
            "ncx": float(cx) / float(sw),
            "ncy": float(cy) / float(sh),
            "nhw": hit_rw * 0.5 / float(sw),
            "nhh": hit_rh * 0.5 / float(sh),
        }
        so = int(canvas.get("sorting_order") or 0)
        if source == "ui_tmp":
            so = so + 1  # child text above Image at same Canvas order
        sp = {
            "r": float(color[0]),
            "g": float(color[1]),
            "b": float(color[2]),
            "a": float(color[3]),
            "enabled": 1,
            "has_sprite": True,
            "sorting_layer_id": int(canvas.get("sorting_layer_id") or 0),
            "sorting_layer_yaml": 0,
            "sorting_order": so,
            "source": source,
            "ui_mode": int(canvas.get("render_mode") or 0),
            "ui_cam_file_id": str(canvas.get("camera_file_id") or ""),
            "scale_x": 1.0,
            "scale_y": 1.0,
            "cos_z": 1.0,
            "sin_z": 0.0,
            "m00": 1.0,
            "m01": 0.0,
            "m10": 0.0,
            "m11": 1.0,
            "half_w": hit_rw * px_w * 0.5,
            "half_h": hit_rh * px_h * 0.5,
            "ncx": float(cx) / float(sw),
            "ncy": float(cy) / float(sh),
            "nhw": hit_rw * 0.5 / float(sw),
            "nhh": hit_rh * 0.5 / float(sh),
            # Live RT: draw size / center as fractions of RectTransform
            # (TMP Overflow bake can be larger than the rect).
            "draw_sx": 1.0,
            "draw_sy": 1.0,
            "shift_fx": 0.0,
            "shift_fy": 0.0,
        }
        if extra:
            sp.update(extra)
        o["sprite"] = sp

    for o in objects:
        ui = o.get("ui_image")
        if not ui or not ui.get("has_sprite") or not int(ui.get("enabled", 1)):
            continue
        canvas = _find_canvas(o)
        if not int(canvas.get("enabled", 1)):
            continue
        mode = int(canvas.get("render_mode", 0))
        if mode not in (0, 1):
            continue
        cx, cy, rw, rh = _ui_screen_rect(o, by_xf, sw, sh, rect_cache)
        builtin = bool(ui.get("builtin"))
        img_type = int(ui.get("image_type") or 0)
        ppu_mul = float(ui.get("pixels_per_unit_multiplier") or 1.0)
        preserve = bool(int(ui.get("preserve_aspect") or 0))
        extra = {
            "builtin": builtin,
            "sprite_file_id": int(ui.get("sprite_file_id") or 0),
            "sprite_guid": ("builtin:uisprite" if builtin
                            else ui.get("sprite_guid")),
            "image_type": img_type,
            "preserve_aspect": 1 if preserve else 0,
        }
        if builtin:
            src_w, src_h, src_rgba, border = _builtin_uisprite()
            tex_path = "<builtin:UISprite>"
        else:
            path = asset_guids.get(ui.get("sprite_guid") or "")
            if not path or not path.lower().endswith(".png"):
                continue
            fid = int(ui.get("sprite_file_id") or 0)
            cache_key = (path, fid)
            if cache_key not in png_cache:
                try:
                    cw, ch, crgba, cborder = _load_sprite_rgba(path, fid)
                    png_cache[cache_key] = (cw, ch, crgba, cborder)
                except Exception:
                    png_cache[cache_key] = None
            loaded = png_cache[cache_key]
            if not loaded:
                continue
            src_w, src_h, src_rgba, border = loaded
            tex_path = path
        # Sliced (1): 9-slice fills the rect. Simple (0): stretch, or
        # preserveAspect-fit inside the rect (Unity Image.preserveAspect).
        rect_w, rect_h = abs(float(rw)), abs(float(rh))
        draw_w, draw_h = rect_w, rect_h
        draw_cx, draw_cy = float(cx), float(cy)
        if (img_type != 1 and preserve
                and src_w > 0 and src_h > 0 and draw_w > 1e-6 and draw_h > 1e-6):
            draw_w, draw_h = _fit_preserve_aspect(
                draw_w, draw_h, src_w, src_h)
            pivot = (o.get("rect") or {}).get("pivot") or (0.5, 0.5)
            draw_cx, draw_cy = _preserve_aspect_draw_center(
                cx, cy, rect_w, rect_h, draw_w, draw_h, pivot)
            # Live recompute: fit from rect + src aspect (not baked ncx alone).
            extra["src_aspect"] = float(src_w) / float(src_h)
        if img_type == 1 and any(b > 0 for b in border):
            tw, th, rgba = _bake_sliced_rgba(
                src_rgba, src_w, src_h, border, rw * k, rh * k, ppu_mul / k)
            draw_w, draw_h = rect_w, rect_h
            draw_cx, draw_cy = float(cx), float(cy)
        else:
            tw, th, rgba = _bake_stretched_rgba(
                src_rgba, src_w, src_h, draw_w * k, draw_h * k)
        extra["tex_path"] = tex_path
        extra["tex_w"] = tw
        extra["tex_h"] = th
        extra["tex_rgba"] = rgba
        extra["pixels_per_unit"] = 100.0
        extra["border"] = border
        _apply_layout(
            o, draw_cx, draw_cy, draw_w, draw_h, canvas, "ui",
            (ui.get("r", 1.0), ui.get("g", 1.0),
             ui.get("b", 1.0), ui.get("a", 1.0)),
            extra)
        # Raycast / Button hit uses the full RectTransform (Unity Graphic).
        if o.get("ui_hit") is not None:
            o["ui_hit"] = {
                "cx": float(cx), "cy": float(cy),
                "hw": rect_w * 0.5, "hh": rect_h * 0.5,
                "ncx": float(cx) / float(sw),
                "ncy": float(cy) / float(sh),
                "nhw": rect_w * 0.5 / float(sw),
                "nhh": rect_h * 0.5 / float(sh),
            }

    for o in objects:
        tmp = o.get("ui_tmp")
        if not tmp or not tmp.get("has_font") or not int(tmp.get("enabled", 1)):
            continue
        canvas = _find_canvas(o)
        if not int(canvas.get("enabled", 1)):
            continue
        mode = int(canvas.get("render_mode", 0))
        if mode not in (0, 1):
            continue
        font_path = asset_guids.get(tmp.get("font_guid") or "")
        if not font_path:
            continue
        font = _load_tmp_font_asset(font_path)
        if not font:
            continue
        cx, cy, rw, rh = _ui_screen_rect(o, by_xf, sw, sh, rect_cache)
        # m_fontSize is in local (pre-canvas) units; scale to screen pixels
        # so glyphs match RectTransform lossyScale (e.g. VLG localScale 0.59).
        lw, lh = _ui_local_rect_wh(o, by_xf, sw, sh, rect_cache)
        fs = float(tmp.get("font_size") or 14.0)
        sz = abs(float(rh)) / float(lh) if lh > 1e-6 else 1.0
        tmp["bake"] = {
            "font": font_path, "size": fs * sz * k, "box_w": rw * k,
            "box_h": rh * k, "k": k,
            "auto_size": ((float(tmp["size_min"]) * sz * k,
                           float(tmp["size_max"]) * sz * k)
                          if tmp.get("auto_size") else None)}
        if not (tmp.get("text") or ""):
            # Nothing to draw; the row a script's `.text =` would draw in
            # (`_tmp_text_variants`) is kept aside.
            keep = (o.get("pos"), o.get("ui_hit"))
            _apply_layout(
                o, float(cx), float(cy), rw, rh, canvas, "ui_tmp",
                (tmp.get("r", 1.0), tmp.get("g", 1.0),
                 tmp.get("b", 1.0), tmp.get("a", 1.0)),
                {"builtin": False, "sprite_file_id": 0,
                 "sprite_guid": "tmpbake:empty", "tex_path": "<tmp:>",
                 "tex_w": 1, "tex_h": 1, "tex_rgba": bytes(4),
                 "pixels_per_unit": 100.0, "tmp_empty": True})
            for key, v in zip(("pos", "ui_hit"), keep):
                o.pop(key, None)
                if v is not None:
                    o[key] = v
            continue
        tw, th, rgba, shift_x, shift_y = _bake_tmp(tmp, tmp["text"], font)
        tex_w, tex_h = tw, th
        tw, th, shift_x, shift_y = tw / k, th / k, shift_x / k, shift_y / k
        # Overflow expands the bake; keep authored alignment by shifting center.
        draw_cx = float(cx) + float(shift_x)
        draw_cy = float(cy) + float(shift_y)
        # Live RT rebuilds draw size from RectTransform; store expand / shift
        # as fractions of the authored rect so overflow bakes are not squished
        # into sizeDelta (tex taller than rect → vertical squash).
        rect_w = max(abs(float(rw)), 1e-6)
        rect_h = max(abs(float(rh)), 1e-6)
        bake_guid = "tmpbake:%s:%s" % (
            o.get("go_id") or o.get("name") or "tmp",
            tmp.get("font_guid") or "")
        _apply_layout(
            o, draw_cx, draw_cy, tw, th, canvas, "ui_tmp",
            (tmp.get("r", 1.0), tmp.get("g", 1.0),
             tmp.get("b", 1.0), tmp.get("a", 1.0)),
            {
                "builtin": False,
                "sprite_file_id": 0,
                "sprite_guid": bake_guid,
                "tex_path": "<tmp:%s>" % (tmp.get("text") or ""),
                "tex_w": tex_w,
                "tex_h": tex_h,
                "tex_rgba": rgba,
                "pixels_per_unit": 100.0,
                "draw_sx": float(tw) / rect_w,
                "draw_sy": float(th) / rect_h,
                "shift_fx": float(shift_x) / rect_w,
                "shift_fy": float(shift_y) / rect_h,
            })

    # Unity Canvas: equal sortingOrder draws in hierarchy order (parents
    # before children). Without a nested Override Sorting Canvas, a full-
    # screen menu Image and a child Button Image share the root order —
    # qsort ties then cover the Button while TMP (+1) still shows. Bump
    # each UI sprite above the nearest ancestor UI sprite on the same layer.
    _bump_ui_hierarchy_draw_order(objects, by_xf)
    for o in objects:
        if (o.get("sprite") or {}).get("tmp_empty"):
            o["ui_tmp"]["empty_sprite"] = o.pop("sprite")


def _editor_only_transform_ids(by_id):
    """Transform fileIDs omitted by Unity's EditorOnly tag (incl. descendants).

    Player builds strip GameObjects with ``m_TagString: EditorOnly`` and their
    transform children. PrefabInstance roots with an ``m_TagString`` override
    of EditorOnly are included the same way.
    """
    roots = set()
    for go in by_id.values():
        if go.get("kind") != "GameObject":
            continue
        if (go.get("tag") or "") != "EditorOnly":
            continue
        for mid in re.findall(r"fileID:\s*(\d+)", go.get("raw") or ""):
            rec = by_id.get(mid)
            if rec is not None and rec is not go and rec.get("kind") == "Transform":
                roots.add(str(mid))
                break
    for pi in by_id.values():
        if pi.get("kind") != "PrefabInstance":
            continue
        raw = pi.get("raw") or ""
        tags = re.findall(
            r"propertyPath:\s*m_TagString\s*\n\s*value:\s*(.+)", raw)
        if not tags or tags[-1].strip() != "EditorOnly":
            continue
        pi_id = str(pi.get("file_id") or "")
        if not pi_id:
            continue
        for rec in by_id.values():
            if rec.get("kind") != "Transform":
                continue
            traw = rec.get("raw") or ""
            if not re.search(
                    r"(?m)^\s+m_PrefabInstance:\s*\{fileID:\s*%s\}"
                    % re.escape(pi_id), traw):
                continue
            # Root of the instance: father is the PI's TransformParent.
            father = rec.get("father_id")
            tp = re.search(
                r"(?m)^\s+m_TransformParent:\s*\{fileID:\s*(-?\d+)\}", raw)
            parent = tp.group(1) if tp else "0"
            if str(father or "0") == str(parent):
                roots.add(str(rec["file_id"]))
    father = {}
    for rec in by_id.values():
        if rec.get("kind") != "Transform":
            continue
        xid = str(rec.get("file_id") or "")
        if not xid:
            continue
        fid = rec.get("father_id")
        if fid and str(fid) not in ("0",):
            father[xid] = str(fid)
    cache = {}

    def _under(xf):
        if xf in cache:
            return cache[xf]
        if xf in roots:
            cache[xf] = True
            return True
        p = father.get(xf)
        if not p:
            cache[xf] = False
            return False
        cache[xf] = _under(p)
        return cache[xf]

    out = set()
    for rec in by_id.values():
        if rec.get("kind") != "Transform":
            continue
        xid = str(rec.get("file_id") or "")
        if xid and _under(xid):
            out.add(xid)
    return out


def parse_unity_yaml(text, guid_to_script=None, asset_guids=None):
    """A Unity .unity YAML subset: GameObject + Transform + MonoBehaviour.

    Also imports authored Camera (!u!20), SpriteRenderer (!u!212), Canvas
    (!u!223), uGUI Image / Button (builtin MB), RectTransform anchors/size,
    Rigidbody2D (!u!50), Rigidbody (!u!54), BoxCollider2D (!u!61),
    CircleCollider2D (!u!58), CapsuleCollider2D (!u!70), BoxCollider (!u!65),
    SphereCollider (!u!135),
    AudioSource (!u!82), Animation (!u!111), Animator (!u!95),
    PhysicsMaterial2D / PhysicMaterial, and AnimationClip / AnimatorController
    assets. Does not invent any of those — missing components stay missing.
    GameObjects with the EditorOnly tag (and their transform descendants) are
    omitted, matching Unity player builds.
    Returns (objects, lights, cameras, hierarchy).
    """
    guid_to_script = guid_to_script or {}
    asset_guids = asset_guids or {}
    mats2d, mats3d = _load_physics_materials(asset_guids)
    anim_clips, anim_controllers = _load_animation_assets(asset_guids)
    objects = []
    lights = []
    cameras = []
    hierarchy = []  # all authored GOs (name + xf) for Transform.Find
    blocks = re.split(r"(?m)^---\s+", text)
    by_id = {}
    # Nested prefab parses fill this; do not clear mid-scene (cache by path).
    # Callers that mutate .prefab files across packs should restart the process.
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
            r"Rigidbody|BoxCollider2D|CircleCollider2D|CapsuleCollider2D|"
            r"PolygonCollider2D|BoxCollider|"
            r"SphereCollider|Animation|Animator|Canvas|AudioSource|"
            r"HingeJoint2D|DistanceJoint2D|SpringJoint2D|FixedJoint2D|"
            r"SliderJoint2D|WheelJoint2D|FrictionJoint2D|RelativeJoint2D|"
            r"TargetJoint2D|ParticleSystem|LineRenderer|MeshFilter|MeshRenderer|Mesh):",
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
        elif type_id == "223":
            kind = "Canvas"
        elif type_id == "50":
            kind = "Rigidbody2D"
        elif type_id == "54":
            kind = "Rigidbody"
        elif type_id == "61":
            kind = "BoxCollider2D"
        elif type_id == "58":
            kind = "CircleCollider2D"
        elif type_id == "70":
            kind = "CapsuleCollider2D"
        elif type_id == "60":
            kind = "PolygonCollider2D"
        elif type_id == "65":
            kind = "BoxCollider"
        elif type_id == "135":
            kind = "SphereCollider"
        elif type_id == "111":
            kind = "Animation"
        elif type_id == "95":
            kind = "Animator"
        elif type_id == "82":
            kind = "AudioSource"
        elif type_id in ("4", "224"):
            kind = "Transform"
        rec = {"file_id": file_id, "kind": kind, "raw": block, "fields": {}}
        nm = re.search(r"(?m)^\s+m_Name:\s*(.+)$", block)
        if nm:
            rec["name"] = nm.group(1).strip()
        if kind == "GameObject":
            act = re.search(r"(?m)^\s+m_IsActive:\s*(\d+)\s*$", block)
            rec["active"] = int(act.group(1)) if act else 1
            lay = re.search(r"(?m)^\s+m_Layer:\s*(\d+)\s*$", block)
            rec["layer"] = int(lay.group(1)) if lay else 0
        tag = re.search(r"(?m)^\s+m_TagString:\s*(.+)$", block)
        if tag:
            rec["tag"] = tag.group(1).strip()
        # GameObject.activeSelf — authored m_IsActive (default active).
        if kind == "GameObject":
            ia = re.search(r"(?m)^\s+m_IsActive:\s*(\d+)", block)
            rec["active"] = int(ia.group(1)) if ia else 1
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
        # Ordered child Transforms (layout groups / sibling index).
        chm = re.search(
            r"(?m)^\s+m_Children:\s*\n((?:[ \t]+-\s*\{fileID:\s*\d+\}\s*\n)*)",
            block)
        if chm:
            rec["child_ids"] = re.findall(
                r"fileID:\s*(\d+)", chm.group(1))
        # RectTransform layout (uGUI) — kept even when kind collapses to Transform.
        if type_id == "224" or "m_AnchorMin:" in block:
            rec["rect"] = {
                "anchor_min": _yaml_vec2(block, "m_AnchorMin", (0.0, 0.0)),
                "anchor_max": _yaml_vec2(block, "m_AnchorMax", (1.0, 1.0)),
                "anchored_position": _yaml_vec2(
                    block, "m_AnchoredPosition", (0.0, 0.0)),
                "size_delta": _yaml_vec2(block, "m_SizeDelta", (0.0, 0.0)),
                "pivot": _yaml_vec2(block, "m_Pivot", (0.5, 0.5)),
            }
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
            ia = re.search(
                r"propertyPath:\s*m_IsActive\s*\n\s*value:\s*(\d+)", block)
            if ia:
                rec["active"] = int(ia.group(1))
        gm = re.search(r"guid:\s*([0-9a-fA-F]+)", block)
        if gm:
            rec["guid"] = gm.group(1).lower()
        for fm in re.finditer(r"(?m)^\s{2}(\w+):\s+(%s)\s*$" % _YAML_NUM,
                              block):
            key = fm.group(1)
            if key.startswith("m_"):
                continue
            val = fm.group(2)
            rec["fields"][key] = float(val) if re.search(r"[.eEIN]", val) else int(val)
        # A LayerMask field: `ground: {serializedVersion: 2, m_Bits: 256}`.
        for fm in re.finditer(r"(?m)^\s{2}(\w+):\s*\{serializedVersion:\s*\d+,"
                              r"\s*m_Bits:\s*(\d+)\s*\}", block):
            if not fm.group(1).startswith("m_"):
                rec["fields"][fm.group(1)] = int(fm.group(2)) & 0xFFFFFFFF
        # Every plain scalar as its text too: a `string` field's value
        # (`label: hello`, `'two words'`). Kept apart from `fields`, which
        # holds numbers -- "007" is not 7 -- and read only for fields the
        # script declares `string`.
        for fm in re.finditer(r"(?m)^\s{2}(\w+):[ \t]*(.*?)[ \t]*$"
                              + _YAML_FLOW_MORE, block):
            key = fm.group(1)
            if key.startswith("m_"):
                continue
            sv = _yaml_scalar_text(_yaml_flow_join(fm.group(2), fm.group(3)))
            if sv is not None:
                rec.setdefault("str_fields", {})[key] = sv
        # Vector2 serialized fields: name: {x: A, y: B}
        # y stops at ',' so Vector3 `{x,y,z}` does not match as Vector2.
        for fm in re.finditer(
                r"(?m)^\s{2}(\w+):\s+\{x:\s*([^,}]+),\s*y:\s*([^,}]+)\}\s*$",
                block):
            key = fm.group(1)
            if key.startswith("m_"):
                continue
            rec.setdefault("vec2_fields", {})[key] = (
                float(fm.group(2)), float(fm.group(3)))
        # Vector3: name: {x: A, y: B, z: C}
        for fm in re.finditer(
                r"(?m)^\s{2}(\w+):\s+\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
                r"\s*z:\s*([^,}]+)\}\s*$",
                block):
            key = fm.group(1)
            if key.startswith("m_"):
                continue
            rec.setdefault("vec3_fields", {})[key] = (
                float(fm.group(2)), float(fm.group(3)), float(fm.group(4)))
        # Transform / component object refs: name: {fileID: N}
        for fm in re.finditer(
                r"(?m)^\s{2}(\w+):\s+\{fileID:\s*(-?\d+)\}\s*$", block):
            key = fm.group(1)
            if key.startswith("m_"):
                continue
            fid = fm.group(2)
            if fid == "0":
                continue
            rec.setdefault("object_refs", {})[key] = fid
        # another asset's object (a prefab's component): "fid@guid", which
        # only an unplaced prefab's row answers to (its mb_ids)
        for fm in re.finditer(
                r"(?m)^\s{2}(\w+):\s+\{fileID:\s*(-?\d+),\s*guid:\s*"
                r"([0-9a-fA-F]+),\s*type:\s*\d+\}\s*$", block):
            if not fm.group(1).startswith("m_") and fm.group(2) != "0":
                rec.setdefault("object_refs", {})[fm.group(1)] = "%s@%s" % (
                    fm.group(2), fm.group(3).lower())
        # Object ref arrays: name:\n  - {fileID: N} ... (0 = null slot).
        for fm in re.finditer(
                r"(?m)^\s{2}(\w+):[ \t]*\n((?:\s{2}- \{fileID:\s*-?\d+"
                r"[^}\n]*\}[ \t]*\n?)+)", block):
            if not fm.group(1).startswith("m_"):
                # an element of another asset (a prefab) is "fid@guid", as a single reference is
                rec.setdefault("object_ref_arrays", {})[fm.group(1)] = [
                    ("%s@%s" % (a, g.lower()) if g and a != "0" else a) for a, g in
                    re.findall(r"fileID:\s*(-?\d+)(?:,\s*guid:\s*([0-9a-fA-F]+))?", fm.group(2))]
        sa = _embedded_arrays(block)
        if sa:
            rec["struct_arrays"] = sa
        # Number arrays: name:\n  - 0.5\n  - 1 ...
        for fm in re.finditer(
                r"(?m)^\s{2}(\w+):[ \t]*\n((?:\s{2}- [-+.\deE]+[ \t]*\n?)+)",
                block):
            if not fm.group(1).startswith("m_"):
                rec.setdefault("num_arrays", {})[fm.group(1)] = [
                    float(v) for v in re.findall(r"- ([-+.\deE]+)", fm.group(2))]
        sv = _embedded_values(block)
        if sv:
            rec["struct_values"] = sv
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
                r"\s*b:\s*([^,}]+),\s*a:\s*([^}]+)\}", block)
            if not col:
                col = re.search(
                    r"m_Color:\s*\{r:\s*([^,}]+),\s*g:\s*([^,}]+),"
                    r"\s*b:\s*([^,}]+)", block)
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            # Unity null sprite is m_Sprite: {fileID: 0} — do not invent a draw.
            spr = re.search(
                r"m_Sprite:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*"
                r"([0-9a-fA-F]+))?",
                block)
            sid = re.search(r"(?m)^\s+m_SortingLayerID:\s*(-?\d+)", block)
            sl = re.search(r"(?m)^\s+m_SortingLayer:\s*(-?\d+)", block)
            so = re.search(r"(?m)^\s+m_SortingOrder:\s*(-?\d+)", block)
            has_sprite = False
            if spr and int(spr.group(1)) != 0:
                g = spr.group(2).lower() if spr.group(2) else None
                # Must resolve to a project asset — no invent / dangling guid.
                has_sprite = bool(g and g in asset_guids)
            mat = re.search(r"(?m)^  m_Materials:\s*\n\s+- \{fileID:\s*-?\d+,\s*guid:\s*"
                            r"([0-9a-fA-F]+)", block)
            rec["sprite"] = {
                # URP's Sprite-Lit-Default: lit by the 2D lights
                "lit": int(bool(mat) and mat.group(1).lower()
                           == "a97c105638bdf8b4a8650670310a4cd3"),
                "r": float(col.group(1)) if col else 1.0,
                "g": float(col.group(2)) if col else 1.0,
                "b": float(col.group(3)) if col else 1.0,
                "a": (float(col.group(4)) if col and col.lastindex >= 4
                      else 1.0),
                "enabled": int(en.group(1)) if en else 1,
                "has_sprite": has_sprite,
                "sprite_file_id": int(spr.group(1)) if spr else 0,
                "sprite_guid": (spr.group(2).lower()
                                if spr and spr.group(2) else None),
                "sorting_layer_id": int(sid.group(1)) if sid else 0,
                "sorting_layer_yaml": int(sl.group(1)) if sl else 0,
                "sorting_order": int(so.group(1)) if so else 0,
            }
        if kind == "Canvas":
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            rm = re.search(r"(?m)^\s+m_RenderMode:\s*(\d+)", block)
            cam = re.search(r"(?m)^\s+m_Camera:\s*\{fileID:\s*(-?\d+)\}", block)
            pd = re.search(r"(?m)^\s+m_PlaneDistance:\s*([0-9.eE+-]+)", block)
            sid = re.search(r"(?m)^\s+m_SortingLayerID:\s*(-?\d+)", block)
            so = re.search(r"(?m)^\s+m_SortingOrder:\s*(-?\d+)", block)
            ovs = re.search(r"(?m)^\s+m_OverrideSorting:\s*(\d+)", block)
            rec["canvas"] = {
                "enabled": int(en.group(1)) if en else 1,
                "render_mode": int(rm.group(1)) if rm else 0,
                "camera_file_id": int(cam.group(1)) if cam else 0,
                "plane_distance": float(pd.group(1)) if pd else 100.0,
                "sorting_layer_id": int(sid.group(1)) if sid else 0,
                "sorting_order": int(so.group(1)) if so else 0,
                "override_sorting": int(ovs.group(1)) if ovs else 0,
            }
        if kind == "MonoBehaviour":
            # Builtin uGUI Image — not a project .cs, but authored scene UI.
            g = None
            gm2 = re.search(
                r"m_Script:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-fA-F]+)",
                block)
            if gm2:
                g = gm2.group(1).lower()
            if _is_ui_image_mb(block, g):
                col = re.search(
                    r"m_Color:\s*\{r:\s*([^,}]+),\s*g:\s*([^,}]+),"
                    r"\s*b:\s*([^,}]+),\s*a:\s*([^}]+)\}", block)
                if not col:
                    col = re.search(
                        r"m_Color:\s*\{r:\s*([^,}]+),\s*g:\s*([^,}]+),"
                        r"\s*b:\s*([^,}]+)", block)
                en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
                spr = re.search(
                    r"m_Sprite:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*"
                    r"([0-9a-fA-F]+))?",
                    block)
                itype = re.search(r"(?m)^\s+m_Type:\s*(\d+)", block)
                ppum = re.search(
                    r"(?m)^\s+m_PixelsPerUnitMultiplier:\s*([0-9.eE+-]+)",
                    block)
                preserv = re.search(
                    r"(?m)^\s+m_PreserveAspect:\s*(\d+)", block)
                has_sprite = False
                builtin = False
                sg = None
                fid = 0
                if spr and int(spr.group(1)) != 0:
                    fid = int(spr.group(1))
                    sg = spr.group(2).lower() if spr.group(2) else None
                    if sg and sg in asset_guids:
                        has_sprite = True
                    elif _is_unity_builtin_guid(sg):
                        # Unity builtin UISprite / Background / Knob, …
                        has_sprite = True
                        builtin = True
                rec["ui_image"] = {
                    "r": float(col.group(1)) if col else 1.0,
                    "g": float(col.group(2)) if col else 1.0,
                    "b": float(col.group(3)) if col else 1.0,
                    "a": (float(col.group(4)) if col and col.lastindex >= 4
                          else 1.0),
                    "enabled": int(en.group(1)) if en else 1,
                    "has_sprite": has_sprite,
                    "builtin": builtin,
                    "sprite_file_id": fid,
                    "sprite_guid": sg,
                    # PrefabInstance m_Sprite mods target this MB fileID.
                    "mb_file_id": file_id,
                    # 0 Simple, 1 Sliced, 2 Tiled, 3 Filled
                    "image_type": int(itype.group(1)) if itype else 0,
                    "preserve_aspect": (
                        int(preserv.group(1)) if preserv else 0),
                    "pixels_per_unit_multiplier": (
                        float(ppum.group(1)) if ppum else 1.0),
                }
            elif _is_ui_button_mb(block, g):
                rec["ui_button"] = _parse_ui_button(block, file_id)
            elif _is_ui_slider_mb(block, g):
                rec["ui_slider"] = _parse_ui_slider(block, file_id)
            elif _is_ui_scrollbar_mb(block, g):
                rec["ui_scrollbar"] = _parse_ui_scrollbar(block, file_id)
            elif _is_ui_scrollrect_mb(block, g):
                rec["ui_scrollrect"] = _parse_ui_scrollrect(block, file_id)
            elif _is_ui_toggle_mb(block, g):
                rec["ui_toggle"] = _parse_ui_toggle(block, file_id)
            elif _is_ui_togglegroup_mb(block, g):
                rec["ui_togglegroup"] = _parse_ui_togglegroup(block, file_id)
            elif _is_ui_eventtrigger_mb(block, g):
                rec["ui_eventtrigger"] = _parse_ui_eventtrigger(block, file_id)
            elif _is_ui_tmp_mb(block, g):
                rec["ui_tmp"] = _parse_ui_tmp(block, asset_guids)
                tm = re.search(r"(?m)^  m_text:[ \t]*(.*?)[ \t]*$"
                               + _YAML_FLOW_MORE, block)
                if tm:
                    rec["ui_tmp"]["text"] = _yaml_scalar_text(
                        _yaml_flow_join(tm.group(1), tm.group(2))) or ""
            elif _is_vlayout_mb(block, g):
                rec["layout_group"] = _parse_hv_layout_group(block, True)
            elif _is_hlayout_mb(block, g):
                rec["layout_group"] = _parse_hv_layout_group(block, False)
            elif _is_layout_element_mb(block, g):
                rec["layout_element"] = _parse_layout_element(block)
            elif _is_content_size_fitter_mb(block, g):
                rec["content_size_fitter"] = _parse_content_size_fitter(block)
            elif _is_aspect_ratio_fitter_mb(block, g):
                rec["aspect_ratio_fitter"] = _parse_aspect_ratio_fitter(block)
            elif _is_canvas_scaler_mb(block, g):
                rec["canvas_scaler"] = _parse_canvas_scaler(block)
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
            cull = re.search(
                r"m_CullingMask:\s*\n\s+serializedVersion:\s*\d+\s*\n"
                r"\s+m_Bits:\s*(-?\d+)", block)
            vp = re.search(
                r"m_NormalizedViewPortRect:\s*\n(?:\s+serializedVersion:.*\n)?"
                r"\s+x:\s*(\S+)\s*\n\s+y:\s*(\S+)\s*\n\s+width:\s*(\S+)\s*\n"
                r"\s+height:\s*(\S+)", block)
            num = lambda k, d: float(  # noqa: E731
                (re.search(r"(?m)^\s+%s:\s*(-?[0-9.eE+-]+)" % k, block)
                 or [0, d])[1])
            rec["camera"] = {
                "enabled": int(num("m_Enabled", 1)),
                "depth": num("m_Depth", 0),
                # 1 Skybox, 2 Solid Color: cleared; 3 Depth only, 4 Nothing
                "clear": int(num("m_ClearFlags", 1)) in (1, 2),
                "viewport": tuple(float(v) for v in vp.groups())
                if vp else (0.0, 0.0, 1.0, 1.0),
                "orthographic": int(ortho.group(1)) if ortho else 1,
                "orthographic_size": (
                    float(osize.group(1)) if osize else 5.0),
                "bg_r": float(bg.group(1)) if bg else 0.05,
                "bg_g": float(bg.group(2)) if bg else 0.05,
                "bg_b": float(bg.group(3)) if bg else 0.08,
                # Unity defaults when YAML omits clip planes.
                "near_clip": float(near.group(1)) if near else 0.3,
                "far_clip": float(far.group(1)) if far else 1000.0,
                "culling_mask": int(cull.group(1)) & 0xFFFFFFFF
                if cull else 0xFFFFFFFF,
            }
        if kind in _JOINT2D_KINDS:
            rec["joint2d"] = _parse_joint2d(kind, block)
        if kind == "MonoBehaviour" and "m_LightType:" in block:
            import tools.unity.unity_pack_gpu2d as _gpu
            _l2d = _gpu.parse_light2d(block)
            if _l2d:
                rec["light2d"] = _l2d
        if kind == "ParticleSystem":
            rec["particle_system"] = _parts.parse_particle_system(block)
        if kind == "Mesh":
            try:
                rec["mesh"] = _mesh.parse_mesh(block)
            except _mesh.MeshError as e:
                raise PackError("Mesh &%s: %s" % (file_id, e))
        if kind == "LineRenderer":
            try:
                rec["line_renderer"] = _lines.parse_line_renderer(block)
            except (_lines.LineError, _curves.CurveError) as e:
                raise PackError("LineRenderer &%s: %s" % (file_id, e))
        if kind == "Rigidbody2D":
            bt = re.search(r"(?m)^\s+m_BodyType:\s*(\d+)", block)
            mass = re.search(r"(?m)^\s+m_Mass:\s*([0-9.eE+-]+)", block)
            gs = re.search(r"(?m)^\s+m_GravityScale:\s*([0-9.eE+-]+)", block)
            # Unity 6+: m_LinearDamping; older builds used m_LinearDrag / m_Drag.
            ld = re.search(r"(?m)^\s+m_LinearDamping:\s*([0-9.eE+-]+)", block)
            if not ld:
                ld = re.search(r"(?m)^\s+m_LinearDrag:\s*([0-9.eE+-]+)", block)
            if not ld:
                ld = re.search(r"(?m)^\s+m_Drag:\s*([0-9.eE+-]+)", block)
            vel = re.search(
                r"m_LinearVelocity:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}",
                block)
            # Older YAML used m_Velocity
            if not vel:
                vel = re.search(
                    r"m_Velocity:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}",
                    block)
            cons = re.search(r"(?m)^\s+m_Constraints:\s*(\d+)", block)
            av = re.search(r"(?m)^\s+m_AngularVelocity:\s*([0-9.eE+-]+)", block)
            rec["rigidbody2d"] = {
                # RigidbodyConstraints2D.FreezeRotation is 4
                "freeze_rot": bool(int(cons.group(1)) & 4) if cons else False,
                "ang_vel": float(av.group(1)) if av else 0.0,
                "body_type": int(bt.group(1)) if bt else 0,
                "mass": float(mass.group(1)) if mass else 1.0,
                "gravity_scale": float(gs.group(1)) if gs else 1.0,
                "linear_damping": float(ld.group(1)) if ld else 0.0,
                "vel_x": float(vel.group(1)) if vel else 0.0,
                "vel_y": float(vel.group(2)) if vel else 0.0,
                "material_guid": _parse_material_guid(block),
            }
        if kind == "Rigidbody":
            mass = re.search(r"(?m)^\s+m_Mass:\s*([0-9.eE+-]+)", block)
            ug = re.search(r"(?m)^\s+m_UseGravity:\s*(\d+)", block)
            drag = re.search(r"(?m)^\s+m_Drag:\s*([0-9.eE+-]+)", block)
            if not drag:
                drag = re.search(
                    r"(?m)^\s+m_LinearDamping:\s*([0-9.eE+-]+)", block)
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
                "drag": float(drag.group(1)) if drag else 0.0,
                "vel_x": float(vel.group(1)) if vel else 0.0,
                "vel_y": float(vel.group(2)) if vel else 0.0,
                "vel_z": float(vel.group(3)) if vel else 0.0,
                "material_guid": _parse_material_guid(block),
            }
        if kind in ("BoxCollider2D", "CapsuleCollider2D"):
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            trig = re.search(r"(?m)^\s+m_IsTrigger:\s*(\d+)", block)
            off = re.search(
                r"m_Offset:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}", block)
            sz = re.search(
                r"m_Size:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}", block)
            # CapsuleCollider2D m_Direction: 0 Vertical, 1 Horizontal.
            dirn = re.search(r"(?m)^\s+m_Direction:\s*(\d+)", block)
            rec["collider2d"] = {
                "kind": "box" if kind == "BoxCollider2D" else (
                    "capsule_h" if dirn and dirn.group(1) == "1"
                    else "capsule_v"),
                "enabled": int(en.group(1)) if en else 1,
                "is_trigger": int(trig.group(1)) if trig else 0,
                "offset_x": float(off.group(1)) if off else 0.0,
                "offset_y": float(off.group(2)) if off else 0.0,
                "size_x": float(sz.group(1)) if sz else 1.0,
                "size_y": float(sz.group(2)) if sz else 1.0,
                "material_guid": _parse_material_guid(block),
            }
            er = re.search(r"(?m)^\s+m_EdgeRadius:\s*([^\s]+)", block)
            if kind == "BoxCollider2D" and er:
                rec["collider2d"]["edge_radius"] = float(er.group(1))
        if kind == "PolygonCollider2D":
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            trig = re.search(r"(?m)^\s+m_IsTrigger:\s*(\d+)", block)
            off = re.search(
                r"m_Offset:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}", block)
            paths = []
            pm = re.search(r"(?m)^[ \t]+m_Paths:\n((?:[ \t]+-.*\n)*)", block)
            for line in (pm.group(1) if pm else "").splitlines():
                pt = re.search(r"\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}", line)
                if not pt:
                    continue
                if re.match(r"\s*-\s+-", line) or not paths:
                    paths.append([])
                paths[-1].append((float(pt.group(1)), float(pt.group(2))))
            rec["collider2d"] = {
                "kind": "polygon",
                "enabled": int(en.group(1)) if en else 1,
                "is_trigger": int(trig.group(1)) if trig else 0,
                "offset_x": float(off.group(1)) if off else 0.0,
                "offset_y": float(off.group(2)) if off else 0.0,
                "paths": paths,
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
        if kind == "AudioSource":
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            poa = re.search(r"(?m)^\s+m_PlayOnAwake:\s*(\d+)", block)
            vol = re.search(r"(?m)^\s+m_Volume:\s*([0-9.eE+-]+)", block)
            pitch = re.search(r"(?m)^\s+m_Pitch:\s*([0-9.eE+-]+)", block)
            loop = re.search(r"(?m)^\s+Loop:\s*(\d+)", block)
            mute = re.search(r"(?m)^\s+Mute:\s*(\d+)", block)
            clip_g = _parse_asset_guid_ref(block, "m_audioClip")
            if not clip_g:
                clip_g = _parse_asset_guid_ref(block, "m_Resource")
            rec["audiosource"] = {
                "enabled": int(en.group(1)) if en else 1,
                "play_on_awake": int(poa.group(1)) if poa else 1,
                "volume": float(vol.group(1)) if vol else 1.0,
                "pitch": float(pitch.group(1)) if pitch else 1.0,
                "loop": int(loop.group(1)) if loop else 0,
                "mute": int(mute.group(1)) if mute else 0,
                "clip_guid": clip_g or "",
            }
        part = {"1839735485": "tilemap", "483693784": "tilemap_renderer",
                "156049354": "grid"}.get(type_id)
        if part:
            try:
                rec["tilemap_part"] = part
                rec[part] = (_tm.parse_tilemap(block) if part == "tilemap"
                             else _tm.parse_renderer(block)
                             if part == "tilemap_renderer"
                             else _tm.parse_grid(block, file_id))
            except _tm.TilemapError as e:
                raise PackError(str(e))
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
    editor_only_xfs = _editor_only_transform_ids(by_id)
    tiles = _tm.bake_tiles(
        by_id, lambda xf: _resolve_world_trs(xf, by_id, world_cache),
        _quat_xy_basis)

    # PrefabInstance m_AddedComponents (and any MB whose m_GameObject points
    # at a stripped GO) are not listed on the GO's m_Component — reverse-map
    # them so DeactivateBasedOnInputDevice / nested scripts still pack, and a
    # prefab variant's added components (a Rigidbody2D) are its object's.
    # (every component kind: a variant's added Rigidbody2D or collider is as unlisted as an added script)
    mbs_by_go = {}
    for rec in by_id.values():
        if rec.get("kind") in (None, "GameObject", "PrefabInstance"):
            continue
        raw = rec.get("raw") or ""
        gm = re.search(
            r"(?m)^\s+m_GameObject:\s*\{fileID:\s*(\d+)\}", raw)
        if gm:
            mbs_by_go.setdefault(gm.group(1), []).append(rec)
    # A plain Transform a script field names (waypoints) stays a packed row.
    mb_ref_ids = {fid for mbs in mbs_by_go.values() for rec in mbs
                  for fid in list((rec.get("object_refs") or {}).values())
                  + [f for arr in (rec.get("object_ref_arrays") or {}).values()
                     for f in arr]}

    # Transforms inside a tree an Animation / Animator animates: a curve's path
    # may pass through any of them (`S1/S2/../S40`, a worm's segments), and a
    # path resolves through packed objects only -- so one with nothing but a
    # Transform stays a packed row. Dropped, its curves were not played.
    _xf_father, _xf_of_go, _animated_go = {}, {}, set()
    for rec in by_id.values():
        raw = rec.get("raw") or ""
        if rec.get("kind") == "Transform":
            fm = re.search(r"(?m)^\s+m_Father:\s*\{fileID:\s*(-?\d+)\}", raw)
            gm = re.search(r"(?m)^\s+m_GameObject:\s*\{fileID:\s*(\d+)\}", raw)
            _xf_father[str(rec.get("file_id"))] = fm.group(1) if fm else "0"
            if gm:
                _xf_of_go[gm.group(1)] = str(rec.get("file_id"))
        elif rec.get("kind") in ("Animation", "Animator"):
            gm = re.search(r"(?m)^\s+m_GameObject:\s*\{fileID:\s*(\d+)\}", raw)
            if gm:
                _animated_go.add(gm.group(1))
    _animated_xf = {_xf_of_go[g] for g in _animated_go if g in _xf_of_go}
    anim_tree_xf = set()
    for xf in _xf_father:
        f, seen = _xf_father.get(xf, "0"), set()
        while f not in ("0", None) and f not in seen:
            if f in _animated_xf:
                anim_tree_xf.add(xf)
                break
            seen.add(f)
            f = _xf_father.get(f, "0")

    # Join MonoBehaviour + Transform + SpriteRenderer onto the GameObject.
    gos = [r for r in by_id.values() if r.get("kind") == "GameObject"]
    for go in gos:
        kids = []
        seen_kid = set()
        for mid in re.findall(r"fileID:\s*(\d+)", go["raw"]):
            if mid in by_id and by_id[mid] is not go:
                kids.append(by_id[mid])
                seen_kid.add(mid)
        go_fid = str(go.get("file_id") or "")
        for mb in mbs_by_go.get(go_fid, []):
            mid = str(mb.get("file_id") or "")
            if mid and mid not in seen_kid:
                kids.append(mb)
                seen_kid.add(mid)
        pos = (0.0, 0.0, 0.0)
        scale = (1.0, 1.0, 1.0)
        rot = (0.0, 0.0, 0.0, 1.0)
        local_pos = pos
        local_rot = rot
        local_scale = scale
        xf = None
        script = None
        fields = {}
        str_fields = {}
        object_refs = {}
        object_ref_arrays = {}
        num_arrays = {}
        mb_ids = []
        vec2_fields = {}
        vec3_fields = {}
        anim_curves = {}
        struct_values = {}
        struct_arrays = {}
        sprite = None
        ui_image = None
        ui_button = None
        ui_slider = None
        ui_scrollbar = None
        ui_scrollrect = None
        ui_toggle = None
        ui_togglegroups = []
        ui_eventtrigger = None
        ui_tmp = None
        layout_group = None
        layout_element = None
        content_size_fitter = None
        aspect_ratio_fitter = None
        canvas_scaler = None
        canvas = None
        cam = None
        rb2d = None
        joints2d = []
        psys = None
        line_r = None
        mesh_f = None
        mesh_r = None
        lights2d = []
        rb3d = None
        col2d = None
        col3d = None
        anim = None
        animator = None
        audiosources = []
        rect = None
        # Each further project script on this GO is its own object below:
        # its fields and fileID are not the primary one's.
        scripted = [k for k in kids if k.get("kind") == "MonoBehaviour"
                    and k.get("guid") in (guid_to_script or {})]
        prim = next((k for k in reversed(scripted) if k.get("ui_slider")),
                    scripted[0] if scripted else None)
        extra_mbs = [k for k in scripted if k is not prim]
        for k in kids:
            if any(k is e for e in extra_mbs):
                continue
            if k.get("kind") == "Transform":
                xf = k
                if k.get("rect"):
                    rect = dict(k["rect"])
            if k.get("pos"):
                pos = k["pos"]
            if k.get("scale"):
                scale = k["scale"]
            if k.get("rot"):
                rot = k["rot"]
            if k.get("kind") == "MonoBehaviour":
                # A field typed as this script's class, on another object,
                # holds this block's fileID: keep it to resolve that.
                mb_ids.append(str(k.get("file_id")))
                fields.update(k.get("fields") or {})
                str_fields.update(k.get("str_fields") or {})
                object_refs.update(k.get("object_refs") or {})
                object_ref_arrays.update(k.get("object_ref_arrays") or {})
                num_arrays.update(k.get("num_arrays") or {})
                vec2_fields.update(k.get("vec2_fields") or {})
                vec3_fields.update(k.get("vec3_fields") or {})
                struct_values.update(k.get("struct_values") or {})
                struct_arrays.update(k.get("struct_arrays") or {})
                # AnimationCurve fields: keys and wrap modes, serialized
                # inline (tools/unity/unity_pack_curves.py).
                try:
                    anim_curves.update(
                        _curves.parse_curve_fields(k.get("raw") or ""))
                except _curves.CurveError as e:
                    raise PackError(
                        "GameObject `%s`: an AnimationCurve field holds %s"
                        % (go.get("name") or "?", e))
                g = k.get("guid")
                if g and g in guid_to_script:
                    # Prefer authored Slider (_Slider) over companion
                    # _Selectable on the same GO so SetDisplayValue /
                    # OnValueChanged resolve to a packed _Slider instance.
                    if script is None or k.get("ui_slider"):
                        script = guid_to_script[g]
                if k.get("ui_image"):
                    ui_image = dict(k["ui_image"])
                if k.get("ui_button"):
                    ui_button = dict(k["ui_button"])
                if k.get("ui_slider"):
                    ui_slider = dict(k["ui_slider"])
                if k.get("ui_scrollbar"):
                    ui_scrollbar = dict(k["ui_scrollbar"])
                if k.get("ui_scrollrect"):
                    ui_scrollrect = dict(k["ui_scrollrect"])
                if k.get("ui_toggle"):
                    ui_toggle = dict(k["ui_toggle"])
                if k.get("ui_togglegroup"):
                    ui_togglegroups.append(dict(k["ui_togglegroup"]))
                if k.get("ui_eventtrigger"):
                    ui_eventtrigger = dict(k["ui_eventtrigger"])
                if k.get("ui_tmp"):
                    ui_tmp = dict(k["ui_tmp"])
                if k.get("layout_group"):
                    layout_group = dict(k["layout_group"])
                if k.get("layout_element"):
                    layout_element = dict(k["layout_element"])
                if k.get("content_size_fitter"):
                    content_size_fitter = dict(k["content_size_fitter"])
                if k.get("aspect_ratio_fitter"):
                    aspect_ratio_fitter = dict(k["aspect_ratio_fitter"])
                if k.get("canvas_scaler"):
                    canvas_scaler = dict(k["canvas_scaler"])
            if k.get("kind") == "SpriteRenderer" and k.get("sprite"):
                sprite = dict(k["sprite"])
            if k.get("kind") == "Canvas" and k.get("canvas"):
                canvas = dict(k["canvas"])
            if k.get("kind") == "Camera" and k.get("camera"):
                cam = dict(k["camera"], file_id=k.get("file_id"))
            if k.get("kind") == "Rigidbody2D" and k.get("rigidbody2d"):
                rb2d = dict(k["rigidbody2d"])
                rb2d["file_id"] = k.get("file_id")
            if k.get("joint2d"):
                joints2d.append(dict(k["joint2d"], file_id=k.get("file_id")))
            if k.get("particle_system"):
                psys = dict(k["particle_system"], file_id=k.get("file_id"))
            if k.get("line_renderer"):
                line_r = dict(k["line_renderer"], file_id=k.get("file_id"))
            if k.get("kind") == "MeshFilter":
                _mm = re.search(r"(?m)^\s+m_Mesh:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*(\w+))?",
                                k.get("raw") or "")
                if _mm and _mm.group(2) is None:
                    _mrec = by_id.get(_mm.group(1)) or {}
                    if _mrec.get("mesh"):
                        mesh_f = (_mm.group(1), _mrec["mesh"])
                elif _mm and _mm.group(2):
                    raise PackError(
                        "GameObject `%s`: a MeshFilter's mesh asset (guid %s) is not "
                        "packed yet; a mesh saved in the scene is"
                        % (go.get("name"), _mm.group(2)))
            if k.get("kind") == "MeshRenderer":
                _raw = k.get("raw") or ""
                _mat = re.search(r"(?ms)^\s+m_Materials:\s*\n\s*-\s*\{[^}]*guid:\s*(\w+)", _raw)
                _so = re.search(r"(?m)^\s+m_SortingOrder:\s*(-?\d+)", _raw)
                _en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", _raw)
                mesh_r = {"mat_guid": _mat.group(1) if _mat else None,
                          "order": int(_so.group(1)) if _so else 0,
                          "enabled": int(_en.group(1)) if _en else 1}
            if k.get("light2d"):
                lights2d.append(dict(k["light2d"]))
            if k.get("kind") == "Rigidbody" and k.get("rigidbody"):
                rb3d = dict(k["rigidbody"])
                rb3d["file_id"] = k.get("file_id")
            if k.get("kind") in ("BoxCollider2D", "CircleCollider2D",
                                 "CapsuleCollider2D",
                                 "PolygonCollider2D") and k.get(
                    "collider2d"):
                col2d = dict(k["collider2d"], file_id=k.get("file_id"))
            if k.get("kind") in ("BoxCollider", "SphereCollider") and k.get(
                    "collider3d"):
                col3d = dict(k["collider3d"])
            if k.get("kind") == "Animation" and k.get("animation"):
                anim = dict(k["animation"])
            if k.get("kind") == "Animator" and k.get("animator"):
                animator = dict(k["animator"])
            if k.get("kind") == "AudioSource" and k.get("audiosource"):
                a = dict(k["audiosource"])
                a["file_id"] = k.get("file_id")
                audiosources.append(a)
        # Unity player builds omit EditorOnly-tagged GOs (and their children).
        xf_id_early = str(xf["file_id"]) if xf and xf.get("file_id") else None
        if xf_id_early and xf_id_early in editor_only_xfs:
            continue
        if (go.get("tag") or "") == "EditorOnly":
            continue
        # Flatten authored Vector2 YAML into _x/_y for packed members.
        for vk, (vx, vy) in vec2_fields.items():
            fields[vk + "_x"] = vx
            fields[vk + "_y"] = vy
        for vk, (vx, vy, vz) in vec3_fields.items():
            fields[vk + "_x"] = vx
            fields[vk + "_y"] = vy
            fields[vk + "_z"] = vz
        local_pos, local_rot, local_scale = pos, rot, scale
        father_id = xf.get("father_id") if xf else None
        xf_id = xf.get("file_id") if xf else None
        child_ids = list(xf.get("child_ids") or []) if xf else []
        # UI Canvas/Image/TMP layout is baked later from anchors + Screen size.
        if xf is not None and not ui_image and not ui_tmp and not canvas:
            pos, rot, scale = _resolve_world_trs(
                xf["file_id"], by_id, world_cache)
        # Resolve Animation / Animator → clip guid (Animator via controller default).
        # Legacy clips play only on Animation; Mecanim clips only on Animator.
        player = None
        if anim and anim.get("enabled", 1) and anim.get("clip_guid"):
            cg = anim["clip_guid"]
            clip = anim_clips.get(cg)
            if clip and int(clip.get("legacy") or 0):
                player = {
                    "kind": "animation",
                    "clip_guid": cg,
                    "playing": int(anim.get("play_automatically") or 0),
                    "speed": 1.0,
                    "loop": int(clip.get("loop") or 0),
                }
        elif animator and animator.get("enabled", 1) and animator.get(
                "controller_guid"):
            cg = anim_controllers.get(animator["controller_guid"])
            clip = anim_clips.get(cg) if cg else None
            if cg and clip and not int(clip.get("legacy") or 0):
                player = {
                    "kind": "animator",
                    "clip_guid": cg,
                    "playing": 1,  # Animator plays default state
                    "speed": 1.0,
                    "loop": int(clip.get("loop") or 0),
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
            if col2d.get("kind") == "polygon":
                ssx, ssy = float(scale[0]), float(scale[1])
                offx = float(col2d.get("offset_x", 0.0))
                offy = float(col2d.get("offset_y", 0.0))
                paths = [[((x + offx) * ssx, (y + offy) * ssy) for x, y in pth]
                         for pth in col2d.get("paths") or []]
                pts = [q for pth in paths for q in pth] or [(0.0, 0.0)]
                x0, x1 = min(q[0] for q in pts), max(q[0] for q in pts)
                y0, y1 = min(q[1] for q in pts), max(q[1] for q in pts)
                cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
                col2d["ox"], col2d["oy"] = cx, cy
                col2d["hw"], col2d["hh"] = (x1 - x0) * 0.5, (y1 - y0) * 0.5
                col2d["tris"] = [
                    tuple(v for (x, y) in tri for v in (x - cx, y - cy))
                    for tri in _triangulate_paths(paths)]
            elif col2d.get("kind") != "circle":
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
            m00, m01, m10, m11 = _quat_xy_basis(
                rot[0], rot[1], rot[2], rot[3])
            sprite["m00"] = m00
            sprite["m01"] = m01
            sprite["m10"] = m10
            sprite["m11"] = m11
        # SpriteRenderer.color: an undrawn renderer (no sprite) has one too
        spr_color = tuple(float(sprite.get(c, 1.0)) for c in "rgba") \
            if sprite else None
        if spr_color is None and ui_image:
            # Image.color: the same per-GameObject tint
            spr_color = tuple(float(ui_image.get(c, 1.0)) for c in "rgba")
        if not (sprite and sprite.get("enabled", 1)
                and sprite.get("has_sprite")):
            sprite = None
        class_name = None
        if script:
            class_name = _class_name_from_cs(script)
        # Every authored GO with a Transform — Find children need not be packed.
        active = int(go.get("active", 1))
        if xf is not None:
            hierarchy.append({
                "name": go.get("name") or "obj",
                "xf_id": xf_id,
                "father_id": father_id,
                "go_id": go.get("file_id"),
                # Every component on this GO, so an authored field holding
                # one (`public Scrollbar scrollbar`) resolves to the GO.
                "comp_ids": [str(k.get("file_id")) for k in kids
                             if k.get("file_id") is not None],
                "active": active,
                "tag": go.get("tag") or "Untagged",
                "layer": int(go.get("layer") or 0),
                "has_canvas": bool(canvas),
                "has_image": bool(ui_image),
                "has_button": bool(ui_button),
                "has_tmp": bool(ui_tmp),
                "local_pos": local_pos,
                "local_rot": local_rot,
                "local_scale": local_scale,
            })
        if cam is not None:
            cameras.append({
                "name": go.get("name") or "Camera",
                "pos": pos,
                "rot": rot,
                "local_pos": local_pos,
                "father_id": father_id,
                "xf_id": xf_id,
                "main": (go.get("tag") == "MainCamera"
                         or (go.get("name") or "").lower() == "main camera"),
                "orthographic": cam["orthographic"],
                "orthographic_size": cam["orthographic_size"],
                "bg_r": cam["bg_r"],
                "bg_g": cam["bg_g"],
                "bg_b": cam["bg_b"],
                "near_clip": cam["near_clip"],
                "far_clip": cam["far_clip"],
                "culling_mask": cam.get("culling_mask", 0xFFFFFFFF),
                "renders": bool(active and cam.get("enabled", 1)),
                "depth": cam.get("depth", 0.0),
                "cam_file_id": str(cam.get("file_id") or ""),
                "clear": cam.get("clear", True),
                "viewport": cam.get("viewport", (0.0, 0.0, 1.0, 1.0)),
            })
        # Camera-only GOs are not packed as scripted instances.
        if (cam is not None and script is None and sprite is None
                and not rb2d and not rb3d and not col2d and not col3d
                and not player and not canvas and not ui_image
                and not ui_tmp):
            continue
        has_ui_draw = bool(
            (ui_image and ui_image.get("has_sprite"))
            or (ui_tmp and ui_tmp.get("has_font") and (ui_tmp.get("text") or "")))
        has_mb = any(k.get("kind") == "MonoBehaviour" for k in kids)
        ui_scaffold_mb = False
        for k in kids:
            raw = k.get("raw") or ""
            if ("EventSystem" in raw or "InputSystemUIInputModule" in raw
                    or "GraphicRaycaster" in raw or "CanvasScaler" in raw
                    or re.search(r"\bUnityEngine\.UI\.Text\b", raw)):
                ui_scaffold_mb = True
                break
        # Image without sprite / TMP without font: keep as _Rect when a
        # RectTransform exists (PrefabInstance UI Button roots often have
        # Image with m_Sprite: {fileID: 0} but still parent TMP children).
        # EventSystem / legacy UI.Text: drop via ui_scaffold_mb below.
        # Prefab stubs with unresolved MB guids: keep (has_mb).
        if ui_image and not has_ui_draw and script is None and sprite is None:
            if not rb2d and not rb3d and not col2d and not col3d and not player:
                if not canvas and not ui_button and not ui_tmp:
                    if rect is not None:
                        objects.append({
                            "name": go.get("name") or "Rect",
                            "pos": pos,
                            "rot": rot,
                            "local_pos": local_pos,
                            "local_rot": local_rot,
                            "local_scale": local_scale,
                            "father_id": father_id,
                            "xf_id": xf_id,
                            "child_ids": child_ids,
                            "go_id": go.get("file_id"),
                            "active": 1 if int(go.get("active", 1)) else 0,
                            "fields": {},
                            "script": None,
                            "class": "_Rect",
                            "sprite": None,
                            "canvas": None,
                            "rect": rect,
                            "ui_image": ui_image,
                            "ui_button": None,
                            "ui_tmp": None,
                            "layout_group": layout_group,
                            "layout_element": layout_element,
                            "content_size_fitter": content_size_fitter,
                            "aspect_ratio_fitter": aspect_ratio_fitter,
                            "rigidbody2d": None,
                            "rigidbody": None,
                            "collider2d": None,
                            "collider3d": None,
                            "anim_player": None,
                            "ui_scaffold": True,
                        })
                        if xf is not None:
                            # hierarchy already appended above when xf set
                            pass
                    continue
        if ui_tmp and not has_ui_draw and script is None and sprite is None:
            # an empty one a script names stays: it may set the text
            if (not rb2d and not rb3d and not col2d and not col3d and not player
                    and not canvas and not ui_button and not ui_image
                    and not ({str(k.get("file_id")) for k in kids}
                             & mb_ref_ids)):
                continue
        if (script is None and sprite is None and not has_ui_draw
                and not rb2d and not rb3d and cam is None and not col2d
                and not col3d and not player and not canvas
                and (not has_mb or ui_scaffold_mb)
                and not (rect is None and xf_id is not None
                         and {str(xf_id), go_fid} & mb_ref_ids)
                and not (xf_id is not None and str(xf_id) in anim_tree_xf)
                and not (mesh_f and mesh_r)):
            # Plain RectTransform parents (layout containers without a
            # MonoBehaviour) must stay so ContentSizeFitter /
            # AspectRatioFitter / layout groups can read parent size.
            if rect is not None and not ui_scaffold_mb:
                objects.append({
                    "name": go.get("name") or "Rect",
                    "pos": pos,
                    "rot": rot,
                    "local_pos": local_pos,
                    "local_rot": local_rot,
                    "local_scale": local_scale,
                    "father_id": father_id,
                    "xf_id": xf_id,
                    "child_ids": child_ids,
                    "go_id": go.get("file_id"),
                    "active": active,
                    "fields": {},
                    "script": None,
                    "class": "_Rect",
                    "sprite": None,
                    "canvas": None,
                    "rect": rect,
                    "ui_image": None,
                    "ui_button": None,
                    "ui_tmp": None,
                    "layout_group": layout_group,
                    "layout_element": layout_element,
                    "content_size_fitter": content_size_fitter,
                    "aspect_ratio_fitter": aspect_ratio_fitter,
                    "canvas_scaler": canvas_scaler,
                    "rigidbody2d": None,
                    "rigidbody": None,
                    "collider2d": None,
                    "collider3d": None,
                    "anim_player": None,
                    "ui_scaffold": True,
                })
            continue
        # Canvas roots (parent Images) — layout walk only, no tick class.
        if canvas and script is None and sprite is None and not has_ui_draw:
            objects.append({
                "name": go.get("name") or "Canvas",
                "pos": pos,
                "rot": rot,
                "local_pos": local_pos,
                "local_rot": local_rot,
                "local_scale": local_scale,
                "father_id": father_id,
                "xf_id": xf_id,
                "child_ids": child_ids,
                "go_id": go.get("file_id"),
                "active": active,
                "fields": {},
                "script": None,
                "class": "_Canvas",
                "sprite": None,
                "canvas": canvas,
                "rect": rect,
                "ui_image": None,
                "ui_button": None,
                "ui_tmp": None,
                "layout_group": layout_group,
                "layout_element": layout_element,
                "content_size_fitter": content_size_fitter,
                "aspect_ratio_fitter": aspect_ratio_fitter,
                "canvas_scaler": canvas_scaler,
                "rigidbody2d": None,
                "rigidbody": None,
                "collider2d": None,
                "collider3d": None,
                "anim_player": None,
                "ui_scaffold": True,
            })
            continue
        objects.append({
            "name": go.get("name") or "obj",
            "pos": pos,
            "rot": rot,
            "local_pos": local_pos,
            "local_rot": local_rot,
            "local_scale": local_scale,
            "father_id": father_id,
            "xf_id": xf_id,
            "child_ids": child_ids,
            "go_id": go.get("file_id"),
            "active": active,
            "fields": fields,
            "str_fields": str_fields,
            "anim_curves": anim_curves,
            "object_refs": object_refs,
            "object_ref_arrays": object_ref_arrays,
            "num_arrays": num_arrays,
            "struct_values": struct_values,
            "struct_arrays": struct_arrays,
            "mb_ids": mb_ids,
            "own_mb_id": str(prim.get("file_id")) if prim is not None else None,   # the primary script's own MonoBehaviour
            "mb_enabled": _mb_enabled((prim or {}).get("raw") or ""),
            "comp_ids": [str(k.get("file_id")) for k in kids
                         if k.get("file_id") is not None],
            "script": script,
            "class": class_name or _scriptless_packed_class(go.get("name")),
            "sprite": sprite,
            "spr_color": spr_color,
            "canvas": canvas,
            "rect": rect,
            "ui_image": ui_image,
            "ui_button": ui_button,
            "ui_slider": ui_slider,
            "ui_scrollbar": ui_scrollbar,
            "ui_scrollrect": ui_scrollrect,
            "ui_toggle": ui_toggle,
            "ui_togglegroups": ui_togglegroups,
            "ui_eventtrigger": ui_eventtrigger,
            "ui_tmp": ui_tmp,
            "layout_group": layout_group,
            "layout_element": layout_element,
            "content_size_fitter": content_size_fitter,
            "aspect_ratio_fitter": aspect_ratio_fitter,
            "canvas_scaler": canvas_scaler,
            # A joint requires a Rigidbody2D: Unity adds a default one.
            "rigidbody2d": rb2d if rb2d or not joints2d else {
                "body_type": 0, "mass": 1.0, "gravity_scale": 1.0,
                "linear_damping": 0.0, "vel_x": 0.0, "vel_y": 0.0,
                "freeze_rot": False, "ang_vel": 0.0, "file_id": None,
                "auto_added": True},
            "joints2d": joints2d,
            "particle_system": psys,
            "line_renderer": line_r,
            "mesh_draw": ({"key": mesh_f[0], "mesh": mesh_f[1], "mat_guid": mesh_r["mat_guid"],
                           "order": mesh_r["order"]}
                          if mesh_f and mesh_r and mesh_r["enabled"] else None),
            "lights2d": lights2d,
            "rigidbody": rb3d,
            "collider2d": col2d,
            "collider3d": col3d,
            "anim_player": player,
            "animator_controller": (animator or {}).get("controller_guid")
            if (animator or {}).get("enabled", 1) else None,
            "audiosources": audiosources,
        })
        for k in extra_mbs:
            efields = dict(k.get("fields") or {})
            for vk, (vx, vy) in (k.get("vec2_fields") or {}).items():
                efields[vk + "_x"], efields[vk + "_y"] = vx, vy
            for vk, (vx, vy, vz) in (k.get("vec3_fields") or {}).items():
                efields[vk + "_x"], efields[vk + "_y"] = vx, vy
                efields[vk + "_z"] = vz
            try:
                ecurves = _curves.parse_curve_fields(k.get("raw") or "")
            except _curves.CurveError as e:
                raise PackError("GameObject `%s`: an AnimationCurve field "
                                "holds %s" % (go.get("name") or "?", e))
            escript = guid_to_script[k["guid"]]
            objects.append({
                "name": go.get("name") or "obj", "pos": pos, "rot": rot,
                "local_pos": local_pos, "local_rot": local_rot,
                "local_scale": local_scale, "father_id": father_id,
                "xf_id": xf_id, "child_ids": child_ids,
                "go_id": go.get("file_id"), "active": active,
                "fields": efields,
                "str_fields": dict(k.get("str_fields") or {}),
                "anim_curves": ecurves,
                "object_refs": dict(k.get("object_refs") or {}),
                "object_ref_arrays": dict(k.get("object_ref_arrays") or {}),
                "num_arrays": dict(k.get("num_arrays") or {}),
                "vec2_fields": dict(k.get("vec2_fields") or {}),
                "vec3_fields": dict(k.get("vec3_fields") or {}),
                "struct_values": dict(k.get("struct_values") or {}),
                "struct_arrays": dict(k.get("struct_arrays") or {}),
                "mb_ids": [str(k.get("file_id"))],
                "own_mb_id": str(k.get("file_id")),
                "mb_enabled": _mb_enabled(k.get("raw") or ""),
                "comp_ids": [str(c.get("file_id")) for c in kids
                             if c.get("file_id") is not None],
                "script": escript, "class": _class_name_from_cs(escript),
                "sprite": None, "canvas": None, "rect": rect,
                "rigidbody2d": None, "rigidbody": None,
                "collider2d": None, "collider3d": None, "anim_player": None,
                "co_mb": True,
            })
    # Stripped PrefabInstance Transforms are not joined via m_Component, but
    # scene children still m_Father them (e.g. button labels). Register those
    # xfs so activeInHierarchy can walk to inactive layout parents.
    existing_xf = {
        str(h.get("xf_id")) for h in hierarchy if h.get("xf_id") is not None}
    needed_xf = set()
    for h in hierarchy:
        fid = str(h.get("father_id") or "")
        if fid not in ("", "0", "None"):
            needed_xf.add(fid)
    for o in objects:
        fid = str(o.get("father_id") or "")
        if fid not in ("", "0", "None"):
            needed_xf.add(fid)
    pending = set(needed_xf)
    while pending:
        xf = pending.pop()
        if xf in existing_xf:
            continue
        if str(xf) in editor_only_xfs:
            existing_xf.add(xf)
            continue
        rec = by_id.get(xf)
        if not rec or rec.get("kind") != "Transform":
            continue
        raw = rec.get("raw") or ""
        pm = re.search(
            r"(?m)^\s+m_PrefabInstance:\s*\{fileID:\s*(\d+)\}", raw)
        pi = by_id.get(pm.group(1)) if pm else None
        pi_raw = (pi.get("raw") if pi else None) or ""
        names = re.findall(
            r"propertyPath:\s*m_Name\s*\n\s*value:\s*(.+)", pi_raw)
        name = (names[-1].strip() if names
                else (_prefab_root_name(pi_raw, asset_guids)
                      or rec.get("name") or "Prefab"))
        acts = re.findall(
            r"propertyPath:\s*m_IsActive\s*\n\s*value:\s*(\d+)", pi_raw)
        if acts:
            active = int(acts[-1])
        elif pi and "active" in pi:
            active = int(pi["active"])
        else:
            active = int(rec.get("active", 1))
        go_id = None
        if pm:
            for g in by_id.values():
                if g.get("kind") != "GameObject":
                    continue
                graw = g.get("raw") or ""
                if re.search(
                        r"(?m)^\s+m_PrefabInstance:\s*\{fileID:\s*%s\}"
                        % re.escape(pm.group(1)), graw):
                    go_id = g.get("file_id")
                    break
        father_id = rec.get("father_id")
        hierarchy.append({
            "name": name,
            "xf_id": xf,
            "father_id": father_id,
            "go_id": go_id,
            "active": active,
            "has_canvas": False,
            "has_image": False,
            "has_button": False,
            "has_tmp": False,
        })
        existing_xf.add(xf)
        fid = str(father_id or "")
        if fid not in ("", "0", "None") and fid not in existing_xf:
            pending.add(fid)
    _materialize_prefab_instance_ui(
        by_id, objects, asset_guids or {}, editor_only_xfs,
        guid_to_script)
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
    _append_prefab_instance_ui_objects(
        by_id, objects, hierarchy, asset_guids, guid_to_script)
    _annotate_ui_button_onclick_targets(objects, by_id, guid_to_script)
    _annotate_ui_slider_onvaluechanged_targets(objects, by_id, guid_to_script)
    _annotate_ui_scrollbar_onvaluechanged_targets(
        objects, by_id, guid_to_script)
    _annotate_ui_toggle_onvaluechanged_targets(objects, by_id, guid_to_script)
    _annotate_ui_eventtrigger_targets(objects, by_id, guid_to_script)
    # set last: the prefab parses above call this function too
    parse_unity_yaml.tiles = tiles
    # On the records themselves (so a prefab's, parsed here too, have them): its Grid / Tilemap components, and the MonoBehaviours of Unity's
    # own packages on it (EventSystem, an input module, a layout group ...), by script name -- what the CCSharp loader makes as components.
    side = _tm.tilemaps(by_id)
    for rec in by_id.values():
        if rec.get("kind") != "MonoBehaviour":
            continue
        gm = re.search(r"m_Script:\s*\{fileID:\s*-?\d+,\s*guid:\s*([0-9a-fA-F]+)", rec.get("raw") or "")
        path = (asset_guids or {}).get(gm.group(1).lower()) if gm else None
        go = _tm._go_of(rec)
        if go is None or not path or not path.endswith(".cs") or not re.search(r"(^|[\\/])(Packages|PackageCache)[\\/]", path):
            continue
        side.setdefault(go, {}).setdefault("builtin_mbs", []).append(
            (os.path.splitext(os.path.basename(path))[0], str(rec.get("file_id")), int(_mb_enabled(rec.get("raw") or ""))))
    # every component that names its GameObject, which a PrefabInstance's added components (on a stripped GameObject) are not listed on: a
    # reference to one resolves through them; an added AudioSource is made too
    named = {}
    for rec in by_id.values():
        go = _tm._go_of(rec)
        if go is not None and rec.get("kind") not in (None, "GameObject"):
            named.setdefault(go, []).append(rec)
    for rec in objects + hierarchy:
        go = str(rec.get("go_id"))
        rec.update(side.get(go, {}))
        have = set(str(x) for x in rec.get("comp_ids") or [])
        extra = [c for c in named.get(go, []) if str(c.get("file_id")) not in have]
        if extra:
            rec["comp_ids"] = list(rec.get("comp_ids") or []) + [str(c.get("file_id")) for c in extra]
            heard = set(str(a.get("file_id")) for a in rec.get("audiosources") or [])
            for c in extra:
                if c.get("kind") == "AudioSource" and c.get("audiosource") and str(c.get("file_id")) not in heard:
                    rec["audiosources"] = list(rec.get("audiosources") or []) + [dict(c["audiosource"], file_id=c.get("file_id"))]
    return objects, lights, cameras, hierarchy


_PREFAB_ROOT_NAME_CACHE = {}


def _prefab_root_name(pi_raw, asset_guids):
    """Name of the root GameObject of a PrefabInstance's source prefab.

    Unity names an instance after its prefab's root object unless the scene
    overrides `m_Name`. The root is the GameObject whose Transform /
    RectTransform has no father. None when the prefab cannot be read.
    """
    m = re.search(r"m_SourcePrefab:\s*\{[^}]*guid:\s*([0-9a-fA-F]+)", pi_raw or "")
    if not m:
        return None
    path = (asset_guids or {}).get(m.group(1).lower())
    if not path or not os.path.isfile(path):
        return None
    if path in _PREFAB_ROOT_NAME_CACHE:
        return _PREFAB_ROOT_NAME_CACHE[path]
    name = None
    try:
        text = _read(path)
    except OSError:
        text = ""
    docs = re.split(r"(?m)^--- !u!(\d+) &(\d+)[^\n]*\n", text)
    go_names = {}
    root_go = None
    for i in range(1, len(docs) - 2, 3):
        cls, body = docs[i], docs[i + 2]
        if cls == "1":
            nm = re.search(r"(?m)^\s+m_Name:\s*(.*)$", body)
            if nm:
                go_names[docs[i + 1]] = nm.group(1).strip()
        elif cls in ("4", "224") and root_go is None:
            if re.search(r"(?m)^\s+m_Father:\s*\{fileID:\s*0\}", body):
                g = re.search(r"(?m)^\s+m_GameObject:\s*\{fileID:\s*(\d+)\}", body)
                if g:
                    root_go = g.group(1)
    if root_go is not None:
        name = go_names.get(root_go) or None
    _PREFAB_ROOT_NAME_CACHE[path] = name
    return name


def _materialize_prefab_instance_ui(by_id, objects, asset_guids,
                                    editor_only_xfs=None,
                                    guid_to_script=None):
    """Create drawable Image objects for PrefabInstance roots with sprite mods.

    Scene UI buttons are often PrefabInstances (stripped root Transform +
    m_Sprite overrides). Without a packed object, layout/bake skip them and
    only scene-added labels exist — and those inherit the wrong Canvas sort
    when the parent walk stops at the stripped root.
    """
    asset_guids = asset_guids or {}
    guid_to_script = guid_to_script or {}
    editor_only_xfs = editor_only_xfs or set()
    have_xf = {str(o.get("xf_id")) for o in objects if o.get("xf_id")}
    canvas_by_go = _canvas_by_gameobject(by_id)
    # PrefabInstance id → root stripped Transform (father == PI TransformParent).
    roots = {}
    for rec in by_id.values():
        if rec.get("kind") != "Transform":
            continue
        raw = rec.get("raw") or ""
        pm = re.search(
            r"(?m)^\s+m_PrefabInstance:\s*\{fileID:\s*(\d+)\}", raw)
        if not pm:
            continue
        pi = by_id.get(pm.group(1))
        if not pi or pi.get("kind") != "PrefabInstance":
            continue
        pi_father = str(pi.get("father_id") or "")
        xf_father = str(rec.get("father_id") or "")
        if pi_father and xf_father == pi_father:
            roots[pm.group(1)] = rec
    for pi_id, xf_rec in roots.items():
        xf_id = str(xf_rec.get("file_id") or "")
        if not xf_id or xf_id in have_xf or xf_id in editor_only_xfs:
            continue
        pi = by_id.get(pi_id)
        if not pi:
            continue
        raw = pi.get("raw") or ""
        # Last resolving m_Sprite override (root Image after child removals).
        sprite_guid = None
        sprite_fid = 0
        for m in re.finditer(
                r"propertyPath:\s*m_Sprite\s*\n\s*value:[^\n]*\n\s+"
                r"objectReference:\s*\{fileID:\s*(-?\d+)"
                r"(?:,\s*guid:\s*([0-9a-fA-F]+))?",
                raw):
            fid = int(m.group(1))
            sg = m.group(2).lower() if m.group(2) else None
            if fid == 0 and not sg:
                continue
            if sg and (sg in asset_guids or _is_unity_builtin_guid(sg)):
                sprite_guid = sg
                sprite_fid = fid
        if not sprite_guid:
            continue
        names = re.findall(
            r"propertyPath:\s*m_Name\s*\n\s*value:\s*(.+)", raw)
        name = (names[-1].strip() if names
                else (_prefab_root_name(raw, asset_guids)
                      or xf_rec.get("name") or "Prefab"))
        acts = re.findall(
            r"propertyPath:\s*m_IsActive\s*\n\s*value:\s*(\d+)", raw)
        # Prefer last m_IsActive on the root GO (often after child toggles).
        active = int(acts[-1]) if acts else int(pi.get("active", 1))
        builtin = bool(_is_unity_builtin_guid(sprite_guid))
        has_sprite = builtin or sprite_guid in asset_guids
        go_id = None
        for g in by_id.values():
            if g.get("kind") != "GameObject":
                continue
            graw = g.get("raw") or ""
            if re.search(
                    r"(?m)^\s+m_PrefabInstance:\s*\{fileID:\s*%s\}"
                    % re.escape(pi_id), graw):
                go_id = g.get("file_id")
                break
        presc = re.search(
            r"propertyPath:\s*m_PreserveAspect\s*\n\s*value:\s*(\d+)", raw)
        # Prefab default for UI Button Image is preserveAspect: 1.
        preserve = int(presc.group(1)) if presc else 1
        itype = re.search(
            r"propertyPath:\s*m_Type\s*\n\s*value:\s*(\d+)", raw)
        # Pull Button / UIButton + scene onClick overrides from source prefab.
        ui_button = None
        src_m = re.search(
            r"m_SourcePrefab:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-fA-F]+)",
            raw)
        if src_m:
            pref_guid = src_m.group(1).lower()
            pref_path = asset_guids.get(pref_guid)
            if (pref_path and str(pref_path).lower().endswith(".prefab")
                    and os.path.isfile(pref_path)):
                try:
                    pref_objs, _l, _c, _h = _parsed_prefab_objects(
                        pref_path, guid_to_script, asset_guids)
                    src = next(
                        (o for o in pref_objs if o.get("ui_button")), None)
                    if src is None:
                        src = next(
                            (o for o in pref_objs if o.get("rect")), None)
                    if src and src.get("ui_button"):
                        ui_button = dict(src["ui_button"])
                        ui_button = _apply_ui_button_onclick_mods(
                            ui_button, raw, ui_button.get("mb_file_id"))
                        _annotate_ui_button_onclick_targets(
                            [{"ui_button": ui_button}], by_id, guid_to_script)
                except Exception:
                    ui_button = None
        # Stable go_id for PrefabInstance roots (scene often has no GO stub).
        if go_id is None:
            go_id = "prefabinst:%s:%s" % (pi_id, xf_id)
        # Nested Canvas from PrefabInstance m_AddedComponents (override sort).
        canvas = canvas_by_go.get(str(go_id)) if go_id else None
        objects.append({
            "name": name,
            "pos": pi.get("pos") or (0.0, 0.0, 0.0),
            "rot": pi.get("rot") or (0.0, 0.0, 0.0, 1.0),
            "local_pos": pi.get("pos") or (0.0, 0.0, 0.0),
            "local_rot": pi.get("rot") or (0.0, 0.0, 0.0, 1.0),
            "local_scale": pi.get("scale") or (1.0, 1.0, 1.0),
            "father_id": xf_rec.get("father_id"),
            "xf_id": xf_id,
            "go_id": go_id,
            "active": active,
            "fields": {},
            "object_refs": {},
            "script": None,
            "class": _scriptless_packed_class(name),
            "sprite": None,
            "canvas": canvas,
            "rect": _prefab_mod_rect(raw),
            "ui_image": {
                "r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0,
                "enabled": 1,
                "has_sprite": has_sprite,
                "builtin": builtin,
                "sprite_file_id": sprite_fid,
                "sprite_guid": sprite_guid,
                "image_type": int(itype.group(1)) if itype else 0,
                "pixels_per_unit_multiplier": 1.0,
                "preserve_aspect": preserve,
            },
            "ui_button": ui_button,
            "ui_slider": None,
            "ui_tmp": None,
            "layout_group": None,
            "layout_element": None,
            "content_size_fitter": None,
            "aspect_ratio_fitter": None,
            "canvas_scaler": None,
            "rigidbody2d": None,
            "rigidbody": None,
            "collider2d": None,
            "collider3d": None,
            "anim_player": None,
            "audiosources": [],
        })
        have_xf.add(xf_id)


_prefab_parse_cache = {}


def _parsed_prefab_objects(path, guid_to_script, asset_guids):
    """Parse a .prefab once (cached) into packed-style objects."""
    key = os.path.abspath(path)
    if key not in _prefab_parse_cache:
        # Placeholder blocks recursive re-entry while parsing this prefab.
        _prefab_parse_cache[key] = None
        try:
            _prefab_parse_cache[key] = parse_unity_yaml(
                _read(path), guid_to_script=guid_to_script,
                asset_guids=asset_guids)
        except Exception:
            _prefab_parse_cache.pop(key, None)
            raise
    cached = _prefab_parse_cache[key]
    if cached is None:
        # Nested PrefabInstance of the same asset — return empty.
        return [], [], [], []
    return cached


def _prefab_mod_values(inst_raw, src_file_id):
    """propertyPath → value string for modifications targeting *src_file_id*."""
    out = {}
    if not inst_raw or not src_file_id:
        return out
    for m in re.finditer(
            r"target:\s*\{fileID:\s*%s,[^}]*\}\s*\n"
            r"\s*propertyPath:\s*([^\n]+)\s*\n"
            r"\s*value:\s*([^\n]*)" % re.escape(str(src_file_id)),
            inst_raw):
        out[m.group(1).strip()] = m.group(2).strip()
    return out


def _append_prefab_instance_ui_objects(
        by_id, objects, hierarchy, asset_guids, guid_to_script):
    """Materialize stripped PrefabInstance roots as UI layout parents.

    Scene YAML often keeps only a stripped RectTransform stub for a UI Button
    prefab; added TMP children parent to that fileID. Without a real object
    (rect + father), ``_ui_screen_rect`` falls back to full-screen center and
    VerticalLayoutGroup cannot stack the buttons.
    """
    if not asset_guids:
        return
    existing_xf = {str(o.get("xf_id")) for o in objects if o.get("xf_id")}
    canvas_by_go = _canvas_by_gameobject(by_id)
    # PrefabInstance id → stripped Transform records that reference it.
    stripped_by_inst = {}
    for fid, rec in by_id.items():
        if rec.get("kind") != "Transform":
            continue
        raw = rec.get("raw") or ""
        cso = re.search(
            r"m_CorrespondingSourceObject:\s*\{fileID:\s*(-?\d+),\s*"
            r"guid:\s*([0-9a-fA-F]+)", raw)
        pim = re.search(
            r"(?m)^\s+m_PrefabInstance:\s*\{fileID:\s*(\d+)\}", raw)
        if not cso or not pim:
            continue
        stripped_by_inst.setdefault(pim.group(1), []).append({
            "scene_xf": str(fid),
            "src_xf": cso.group(1),
            "guid": cso.group(2).lower(),
            "rec": rec,
        })

    for inst_id, stubs in stripped_by_inst.items():
        inst = by_id.get(inst_id)
        if not inst or inst.get("kind") != "PrefabInstance":
            continue
        inst_raw = inst.get("raw") or ""
        father_id = inst.get("father_id")
        for stub in stubs:
            xf_id = stub["scene_xf"]
            path = asset_guids.get(stub["guid"])
            if not path or not str(path).lower().endswith(".prefab"):
                continue
            if not os.path.isfile(path):
                continue
            try:
                pref_objs, _l, _c, _h = _parsed_prefab_objects(
                    path, guid_to_script, asset_guids)
            except Exception:
                continue
            src = next(
                (o for o in pref_objs
                 if str(o.get("xf_id")) == stub["src_xf"]),
                None)
            if src is None:
                # Prefab root often matches first object with a rect.
                src = next((o for o in pref_objs if o.get("rect")), None)
            if src is None:
                continue
            ui_button = dict(src["ui_button"]) if src.get("ui_button") else None
            if ui_button:
                ui_button = _apply_ui_button_onclick_mods(
                    ui_button, inst_raw, ui_button.get("mb_file_id"))
                _annotate_ui_button_onclick_targets(
                    [{"ui_button": ui_button}], by_id, guid_to_script)
            ui_slider = dict(src["ui_slider"]) if src.get("ui_slider") else None
            # Image-only materialize may have created this xf already — attach
            # Button / UIButton ColorBlock + onClick from the source prefab.
            if xf_id in existing_xf:
                for o in objects:
                    if str(o.get("xf_id")) != xf_id:
                        continue
                    if ui_button and not o.get("ui_button"):
                        o["ui_button"] = ui_button
                    if ui_slider and not o.get("ui_slider"):
                        o["ui_slider"] = ui_slider
                    # Prefer a stable prefabinst go_id when scene stub
                    # left go_id None (needed by _build_ui_buttons).
                    if o.get("go_id") is None:
                        o["go_id"] = "prefabinst:%s:%s" % (inst_id, xf_id)
                    # Nested Canvas from m_AddedComponents on stripped GO.
                    if not o.get("canvas"):
                        gid = None
                        for g in by_id.values():
                            if g.get("kind") != "GameObject":
                                continue
                            graw = g.get("raw") or ""
                            if re.search(
                                    r"(?m)^\s+m_PrefabInstance:\s*"
                                    r"\{fileID:\s*%s\}"
                                    % re.escape(inst_id), graw):
                                gid = g.get("file_id")
                                o["go_id"] = gid
                                break
                        if gid is None:
                            gid = o.get("go_id")
                        c = canvas_by_go.get(str(gid)) if gid else None
                        if c:
                            o["canvas"] = c
                    break
                continue
            mods = _prefab_mod_values(inst_raw, stub["src_xf"])
            # m_IsActive targets the prefab GameObject fileID, not the RT.
            go_mods = {}
            go_src = src.get("go_id")
            if go_src:
                go_mods = _prefab_mod_values(inst_raw, go_src)
            rect, scale = _apply_rect_property_mods(
                src.get("rect"),
                src.get("local_scale") or src.get("scale") or (1, 1, 1),
                mods)
            active = int(src.get("active", 1))
            if "m_IsActive" in go_mods:
                try:
                    active = int(float(go_mods["m_IsActive"]))
                except ValueError:
                    pass
            name = src.get("name") or "Prefab"
            # Prefer authored GO name overrides if present.
            if "m_Name" in go_mods and go_mods["m_Name"]:
                name = go_mods["m_Name"]
            # Disambiguate duplicate prefab roots (many "UI Button") so
            # go_names / go_parents stay 1:1 with xf_id.
            base = name.split("<", 1)[0]
            name = "%s<%s>" % (base, xf_id)
            ui_image = dict(src["ui_image"]) if src.get("ui_image") else None
            ui_image = _apply_ui_image_sprite_mod(
                ui_image, inst_raw, asset_guids)
            go_id = "prefabinst:%s:%s" % (inst_id, xf_id)
            # Prefer stripped GameObject fileID when present (AddedComponents).
            for g in by_id.values():
                if g.get("kind") != "GameObject":
                    continue
                graw = g.get("raw") or ""
                if re.search(
                        r"(?m)^\s+m_PrefabInstance:\s*\{fileID:\s*%s\}"
                        % re.escape(inst_id), graw):
                    go_id = g.get("file_id")
                    break
            canvas = canvas_by_go.get(str(go_id)) if go_id else None
            obj = {
                "name": name,
                "pos": src.get("pos") or (0.0, 0.0, 0.0),
                "rot": src.get("rot") or (0.0, 0.0, 0.0, 1.0),
                "local_pos": src.get("local_pos") or (0.0, 0.0, 0.0),
                "local_rot": src.get("local_rot") or (0.0, 0.0, 0.0, 1.0),
                "local_scale": scale,
                "scale": scale,
                "father_id": father_id,
                "xf_id": xf_id,
                "go_id": go_id,
                "active": 1 if active else 0,
                "fields": {},
                "script": None,
                "class": "_Rect",
                "sprite": None,
                "canvas": canvas,
                "rect": rect,
                "ui_image": ui_image,
                "ui_button": ui_button,
                "ui_slider": ui_slider,
                "ui_tmp": None,
                "layout_group": None,
                "layout_element": (
                    dict(src["layout_element"])
                    if src.get("layout_element") else None),
                "content_size_fitter": None,
                "aspect_ratio_fitter": None,
                "rigidbody2d": None,
                "rigidbody": None,
                "collider2d": None,
                "collider3d": None,
                "anim_player": None,
                "ui_scaffold": False,
                "prefab_instance": True,
            }
            objects.append(obj)
            hierarchy.append({
                "name": name,
                "xf_id": xf_id,
                "father_id": father_id,
                "go_id": obj["go_id"],
                "active": 1 if active else 0,
                "has_canvas": bool(canvas),
                "has_image": bool(ui_image),
                "has_button": bool(ui_button),
                "has_tmp": False,
            })
            existing_xf.add(xf_id)
            # Also stash rect onto the stripped Transform for any other walks.
            stub["rec"]["rect"] = dict(rect)
            stub["rec"]["scale"] = scale
            if father_id:
                stub["rec"]["father_id"] = father_id


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
            "class": o.get("class") or _scriptless_packed_class(o.get("name")),
            "sprite": o.get("sprite"),
        })
    return out


# Names that already own GameObject_GetComponent_<T> / _engine_go_<T> helpers.
# A scriptless GO named "Button" must not become packed class Button — C has
# no overloading, so the UI helper and the packed-class emit would collide.
_RESERVED_PACKED_CLASS_NAMES = (
    _ADDABLE_BUILTINS | _PHYSICS_COMPONENTS | (_JOINT2D_COMPONENTS | _PARTICLE_COMPONENTS)
    | _UI_GETCOMPONENT_TYPES | _TRANSFORM_GETCOMPONENT_TYPES | _REFUSED_ADDCOMPONENT)


def _scriptless_packed_class(go_name):
    """Packed class for a GameObject with no project MonoBehaviour script.

    Unity GO names are labels, not component types. Reusing the name as a
    packed class is fine for \"Play Button\", but names that match a Unity
    builtin / uGUI type collide with GetComponent_<T> C symbols.
    """
    name = go_name or "Obj"
    if name in _RESERVED_PACKED_CLASS_NAMES:
        return "_Rect"
    return name


_ENGINE_TRI_TYPEDEF = (
    "typedef struct EngineTri {\n"
    "    float x[3], y[3]; /* world */\n"
    "    float u[3], v[3]; /* the texture's, 0..1 */\n"
    "    float r, g, b, a;\n"
    "    int tex; /* engine_texture_* index; -1 if none */\n"
    "    int sorting_layer, sorting_order; /* as EngineDraw's */\n"
    "} EngineTri;\n")


# The more-indented lines after `key: value` (blank ones between them): a
# quoted scalar Unity wrapped over several lines.
_YAML_FLOW_MORE = r"((?:(?:\n[ \t]*)*\n {3,}[^\n]*)*)"


def _yaml_flow_join(raw, more):
    """*raw* and its continuation lines *more* as one line, folded as YAML
    folds a quoted scalar: a line break is a space, each blank line a
    newline. Only an unclosed quote continues; anything else is *raw*."""
    raw = raw.strip()
    if not more or not raw or raw[0] not in "'\"" or (
            len(raw) > 1 and raw.endswith(raw[0])
            and not raw.endswith(raw[0] * 2) or raw.endswith(raw[0] * 3)):
        return raw
    out, breaks = raw, 0
    for line in more.split("\n")[1:]:
        line = line.strip()
        if not line:
            breaks += 1
            continue
        out += ("\n" * breaks if breaks else " ") + line
        breaks = 0
    return out


def _yaml_scalar_text(raw):
    """A Unity YAML scalar as text: plain, 'single' ('' is a quote) or
    "double" (backslash escapes). None for a mapping, a sequence or a
    reference (`{fileID: ..}`), which no string field holds."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "{[&*|>":
        return None
    if raw[0] == "'" and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    if raw[0] == '"' and raw.endswith('"') and len(raw) >= 2:
        out, k, body = [], 0, raw[1:-1]
        esc = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
               "/": "/", "0": "\0"}
        while k < len(body):
            c = body[k]
            if c == "\\" and k + 1 < len(body):
                nx = body[k + 1]
                if nx == "u" and k + 5 < len(body) + 0:
                    try:
                        out.append(chr(int(body[k + 2:k + 6], 16)))
                        k += 6
                        continue
                    except ValueError:
                        pass
                out.append(esc.get(nx, nx))
                k += 2
                continue
            out.append(c)
            k += 1
        return "".join(out)
    return raw


_YAML_DOC_HEAD_RE = re.compile(r"(?m)^--- !u!(\d+) &(-?\d+)( stripped)?[^\n]*$")


_PREFAB_MOD_RE = re.compile(
    r"-\s*target:\s*\{fileID:\s*(-?\d+)[^}]*\}\s*\n\s*propertyPath:\s*(.*?)\s*\n"
    r"\s*value:\s*(.*?)\s*\n\s*objectReference:\s*(\{[^}]*\})")


_FILE_ID_MASK = 0x7FFFFFFFFFFFFFFF


def _yaml_docs(text):
    """``[(class_id, file_id, stripped, start, end)]`` of a Unity YAML file."""
    heads = list(_YAML_DOC_HEAD_RE.finditer(text))
    return [(h.group(1), int(h.group(2)), bool(h.group(3)), h.start(),
             heads[k + 1].start() if k + 1 < len(heads) else len(text))
            for k, h in enumerate(heads)]


def _scene_prefab_instances(scene_text):
    """The PrefabInstance docs of a (fileID-qualified) scene: ``{id,
    prefab_guid, parent, mods, stripped}`` -- *mods* the property overrides
    ``(source id, path, value, objectReference)``, *stripped* the scene
    fileID of each source object the scene keeps a stripped stub for."""
    insts, stripped = {}, {}
    for cls, fid, is_stripped, a, b in _yaml_docs(scene_text):
        doc = scene_text[a:b]
        if is_stripped:
            src = re.search(r"m_CorrespondingSourceObject:\s*\{fileID:\s*(-?\d+)",
                            doc)
            pi = re.search(r"m_PrefabInstance:\s*\{fileID:\s*(\d+)\}", doc)
            if src and pi:
                stripped.setdefault(int(pi.group(1)), {})[
                    int(src.group(1))] = fid
            continue
        if cls != "1001":
            continue
        g = _SOURCE_PREFAB_GUID_RE.search(doc)
        if not g:
            continue
        parent = re.search(r"m_TransformParent:\s*\{fileID:\s*(\d+)\}", doc)
        insts[fid] = {
            "id": fid, "prefab_guid": g.group(1).lower(),
            "parent": int(parent.group(1)) if parent else 0,
            "mods": [(int(m.group(1)), m.group(2).strip("'\""), m.group(3),
                      m.group(4)) for m in _PREFAB_MOD_RE.finditer(doc)],
        }
    for fid, inst in insts.items():
        inst["stripped"] = stripped.get(fid, {})
    return list(insts.values())


def _embedded_arrays(block):
    """``{field: [value, ...]}`` of the arrays of embedded [Serializable] values in *block* (``areaZones:\n  - corners: []\n    collider: ..``),
    each element read as _embedded_values reads one."""
    out = {}
    for fm in re.finditer(r"(?m)^  (\w+):[ \t]*\n((?:  - \w+:[^\n]*(?:\n|$)(?:    [^\n]*(?:\n|$))*)+)", block):
        if fm.group(1).startswith("m_"):
            continue
        items = re.split(r"(?m)^  - ", fm.group(2))[1:]
        vals = []
        for it in items:
            lines = it.rstrip("\n").split("\n")
            body = "\n".join(["    " + lines[0]] + lines[1:]) + "\n"
            vals.append(_embedded_values("  item:\n" + body).get("item", {}))
        out[fm.group(1)] = vals
    return out


def _embedded_values(block):
    """``{field: {"fields", "str_fields", "object_refs", "vec2_fields",
    "struct_values"}}`` of the embedded [Serializable] values in a
    component's YAML *block* -- a field written as a nested mapping
    (``entry:\\n    layer: 1``), read like the component's own fields."""
    out = {}
    for fm in re.finditer(r"(?m)^  (\w+):[ \t]*\n((?:    [^\n]*(?:\n|$))+)",
                          block):
        if fm.group(1).startswith("m_"):
            continue
        sub = re.sub(r"(?m)^  ", "", fm.group(2))
        v = {"fields": {}, "str_fields": {}, "object_refs": {},
             "vec2_fields": {}}
        for m in re.finditer(r"(?m)^  (\w+):[ \t]*(.*?)[ \t]*$"
                             + _YAML_FLOW_MORE, sub):
            key, raw = m.group(1), _yaml_flow_join(m.group(2), m.group(3))
            if re.fullmatch(_YAML_NUM, raw):
                v["fields"][key] = (float(raw) if re.search(r"[.eEIN]", raw)
                                    else int(raw))
            ref = re.fullmatch(r"\{fileID:\s*(-?\d+)\}", raw)
            if ref and ref.group(1) != "0":
                v["object_refs"][key] = ref.group(1)
            # another asset's object (a prefab's component): "fid@guid", as a component's own fields have it
            aref = re.fullmatch(r"\{fileID:\s*(-?\d+),\s*guid:\s*([0-9a-fA-F]+),\s*type:\s*\d+\}", raw)
            if aref and aref.group(1) != "0":
                v["object_refs"][key] = "%s@%s" % (aref.group(1), aref.group(2).lower())
            vec = re.fullmatch(r"\{x:\s*([^,}]+),\s*y:\s*([^,}]+)\}", raw)
            if vec:
                v["vec2_fields"][key] = (float(vec.group(1)),
                                         float(vec.group(2)))
            st = _yaml_scalar_text(raw)
            if st is not None:
                v["str_fields"][key] = st
        # lists of references and of numbers in it, as a component's own are read
        for am in re.finditer(r"(?m)^  (\w+):[ \t]*\n((?:  - \{fileID:\s*-?\d+[^}\n]*\}[ \t]*(?:\n|$))+)", sub):
            v.setdefault("object_ref_arrays", {})[am.group(1)] = [
                ("%s@%s" % (a, g.lower()) if g and a != "0" else a) for a, g in
                re.findall(r"fileID:\s*(-?\d+)(?:,\s*guid:\s*([0-9a-fA-F]+))?", am.group(2))]
        for am in re.finditer(r"(?m)^  (\w+):[ \t]*\n((?:  - [-+.\deE]+[ \t]*(?:\n|$))+)", sub):
            v.setdefault("num_arrays", {})[am.group(1)] = [float(x) for x in re.findall(r"- ([-+.\deE]+)", am.group(2))]
        inner = _embedded_values(sub)
        if inner:
            v["struct_values"] = inner
        arrs = _embedded_arrays(sub)
        if arrs:
            v["struct_arrays"] = arrs
        out[fm.group(1)] = v
    return out


def _set_yaml_property(doc, path, value):
    """*doc* with the serialized property *path* (``name`` or ``a.b`` into
    a flow mapping) set to *value*; unchanged for paths this does not model
    (arrays, nested blocks)."""
    if "Array" in path or not re.fullmatch(r"\w+(?:\.\w+)?", path):
        return doc
    top, _, sub = path.partition(".")
    line = re.search(r"(?m)^  %s:[ \t]*(.*)$" % re.escape(top), doc)
    if not line:
        if sub:
            return doc
        body = doc.rstrip("\n")
        return body + "\n  %s: %s" % (top, value) + doc[len(body):]
    if not line.group(1).strip():
        # `a.b` into an embedded value's block (`a:\n    b: v`)
        blk = re.match(r"(?:\n    [^\n]*)+", doc[line.end():])
        sl = sub and blk and re.search(r"(?m)^    %s:[ \t]*(.*)$"
                                       % re.escape(sub), blk.group(0))
        if not sl:
            return doc
        at = line.end()
        return doc[:at + sl.start(1)] + value + doc[at + sl.end(1):]
    if not sub:
        return doc[:line.start(1)] + value + doc[line.end(1):]
    flow = line.group(1)
    if not (flow.startswith("{") and flow.endswith("}")):
        return doc
    new = re.sub(r"(?<=[{,\s])%s:\s*[^,}]*" % re.escape(sub),
                 "%s: %s" % (sub, value), flow, count=1)
    return doc[:line.start(1)] + new + doc[line.end(1):]


def _prefab_instance_text(prefab_text, inst):
    """The prefab's YAML as the scene instance *inst* places it: local
    fileIDs are the scene's stripped stubs where it has one, else Unity's
    ``(instance ^ source) & mask``; property overrides applied; the root
    Transform under the instance's ``m_TransformParent``."""
    pid, strip = int(inst["id"]), inst.get("stripped") or {}

    def new_id(x):
        if x == 0:
            return 0
        got = strip.get(x)
        return got if got is not None else (x ^ pid) & _FILE_ID_MASK

    text = _LOCAL_FILE_ID_RE.sub(
        lambda m: m.group(1) + str(new_id(int(m.group(2)))), prefab_text)
    docs = {fid: (a, b) for _c, fid, _s, a, b in _yaml_docs(text)}
    edits = {}
    for src, path, value, objref in inst.get("mods") or []:
        fid = new_id(src)
        if fid not in docs:
            continue
        ref = re.match(r"\{fileID:\s*(-?\d+)", objref)
        if ref and ref.group(1) != "0":
            value = objref
        a, b = docs[fid]
        edits[fid] = _set_yaml_property(edits.get(fid, text[a:b]), path, value)
    if inst.get("parent"):
        for cls, fid, _s, a, b in _yaml_docs(text):
            if cls in ("4", "224") and re.search(
                    r"(?m)^  m_Father:\s*\{fileID:\s*0\}", text[a:b]):
                edits[fid] = re.sub(
                    r"(?m)^(  m_Father:\s*)\{fileID:\s*0\}",
                    r"\g<1>{fileID: %d}" % inst["parent"],
                    edits.get(fid, text[a:b]), count=1)
    if not edits:
        return text
    out, pos = [], 0
    for fid, (a, b) in sorted(docs.items(), key=lambda kv: kv[1][0]):
        if fid in edits:
            out.append(text[pos:a])
            out.append(edits[fid])
            pos = b
    out.append(text[pos:])
    return "".join(out)


def _expand_unstripped_prefab_instances(scene_text, assets, depth=0):
    """*scene_text* with the placed objects of every PrefabInstance. A
    stripped stub (kept when scene objects reference a prefab object, e.g.
    children under its Transform) is replaced by the placed doc, which keeps
    the stub's fileID. Instances with a stripped root RectTransform are left to
    `_append_prefab_instance_ui_objects` (onClick array overrides, components
    added on the stripped GameObject). A prefab's own PrefabInstances (a
    variant's base, nested prefabs) are placed in it first, as Unity does."""
    extra, drop = [], set()
    for inst in _scene_prefab_instances(scene_text):
        ppath = (assets or {}).get(inst["prefab_guid"])
        if not ppath or not ppath.lower().endswith(".prefab") \
                or not os.path.isfile(ppath):
            continue
        raw = _read(ppath)
        if depth < 16:
            raw = _expand_unstripped_prefab_instances(raw, assets, depth + 1)
        # a UI prefab (root RectTransform stub); a stub on a child Canvas
        # (Game Camera's) still places the rest of the prefab here
        if any(cls == "224" and fid in inst["stripped"] and re.search(
                r"(?m)^  m_Father:\s*\{fileID:\s*0\}", raw[a:b])
               for cls, fid, _s, a, b in _yaml_docs(raw)):
            continue
        body = _prefab_instance_text(raw, inst)
        first = _YAML_DOC_HEAD_RE.search(body)
        if first:
            extra.append(body[first.start():].rstrip("\n"))
            placed = {fid for _c, fid, _s, _a, _b in _yaml_docs(body)}
            drop |= placed & set(inst["stripped"].values())
    if not extra:
        return scene_text
    if drop:
        scene_text = _drop_yaml_docs(scene_text, drop)
    return scene_text.rstrip("\n") + "\n" + "\n".join(extra) + "\n"


def _drop_yaml_docs(text, file_ids):
    """*text* without the stripped docs whose fileID is in *file_ids*."""
    docs = _yaml_docs(text)
    out = [text[:docs[0][3]] if docs else text]
    out += [text[a:b] for _c, fid, s, a, b in docs
            if not (s and fid in file_ids)]
    return "".join(out)


def _scene_list(root, paths):
    """Build scenes in buildIndex order: ``{name, path}`` (Unity ``Scene.name``
    is the file name without ``.unity``; ``Scene.path`` is project-relative)."""
    return [{
        "name": os.path.splitext(os.path.basename(p))[0],
        "path": os.path.relpath(p, root).replace(os.sep, "/"),
    } for p in paths]


def _load_scenes_lights_cameras(root, assets):
    """Parse scenes / blender JSON → objects, lights, cameras, hierarchy.

    Does not analyze scripts or load prefab extras. Sprite pixels are attached.
    """
    guids = _guid_map(root, asset_guids=assets)
    objects = []
    lights = []
    cameras = []
    hierarchy = []
    scenes = _unity_scenes_to_pack(root, asset_guids=assets)
    _progress("packing %d build scene(s)" % len(scenes))
    tiles = []
    for si, path in enumerate(scenes):
        _progress("  scene %d/%d %s" % (
            si + 1, len(scenes), os.path.relpath(path, root)))
        objs, scene_lights, scene_cams, scene_hier = parse_unity_yaml(
            _expand_unstripped_prefab_instances(
                _scene_local_file_ids(_read(path), si), assets),
            guid_to_script=guids, asset_guids=assets)
        for rec in objs + scene_lights + scene_cams + scene_hier:
            rec["scene"] = si
        tiles += [{"tile": True, "scene": si, "sprite": dict(t, scene=si)}
                  for t in parse_unity_yaml.tiles]
        objects.extend(objs)
        lights.extend(scene_lights)
        cameras.extend(scene_cams)
        hierarchy.extend(scene_hier)
    _load_scenes_lights_cameras.scenes = _scene_list(root, scenes)
    for path in _walk_files(root, (".json",)):
        if os.path.basename(path) == "blender_pack.json":
            objects.extend(parse_blender_json(_read(path)))
    sorting_layers = _load_sorting_layers(root)
    if not objects:
        raise PackError(
            "no scene objects found under %s "
            "(looked for .unity / blender_pack.json)" % root)
    _progress("scene objects=%d lights=%d cameras=%d hierarchy=%d" % (
        len(objects), len(lights), len(cameras), len(hierarchy)))
    _mark_hierarchy_live(objects, hierarchy)
    _apply_camera_script_view_to_cameras(cameras, objects)
    sw, sh = _ui_layout_screen(root, objects)
    _ui_mark_canvas_units(root, objects)
    _apply_layout_groups(objects, sw, sh)
    _apply_slider_visuals(objects)
    _apply_scrollbar_visuals(objects)
    _bake_ui_images(
        objects, cameras, sw, sh, asset_guids=assets, hierarchy=hierarchy,
        pixel_scale=_ui_bake_pixel_scale(root, objects))
    # Snapshot rect onto hierarchy before dropping layout-only scaffolds so
    # live RT / GO parent chains still see Canvas / plain Rect parents.
    _snapshot_ui_rects_onto_hierarchy(objects, hierarchy)
    objects = [o for o in objects if not o.get("ui_scaffold")] + tiles
    _apply_sprite_sorting(objects, sorting_layers)
    _attach_sprite_textures(objects, assets)
    # Bake layout size (CanvasScaler may live only on dropped Canvas scaffold).
    _load_scenes_lights_cameras.ui_layout = (int(sw), int(sh))
    return objects, lights, cameras, hierarchy


_SOURCE_PREFAB_GUID_RE = re.compile(
    r"m_SourcePrefab:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-fA-F]+)")


def project_folder_name(root):
    """Unity project folder name (not productName)."""
    return os.path.basename(os.path.abspath(root).rstrip(os.sep)) or "Player"


def default_pack_dir(root):
    """$TMPDIR/<project folder> — default place for sources and the player."""
    import tempfile
    return os.path.join(tempfile.gettempdir(), project_folder_name(root))


_ENGINE_CAMERA_TYPEDEF = """typedef struct EngineCamera {
    float x, y;                         /* world position: the view's centre */
    float half_h;                       /* orthographicSize */
    float aspect;                       /* Camera.aspect; 0: the viewport's */
    float rect_x, rect_y, rect_w, rect_h; /* Camera.rect, normalized */
    float bg_r, bg_g, bg_b;             /* backgroundColor */
    int clear;                          /* 1: clear to bg first (Solid Color / Skybox) */
} EngineCamera;
"""


def emit_engine_draw_h():
    return (
        "/* generated by tools/unity/unity_pack.py — do not edit */\n"
        "#ifndef UNITY_PACK_ENGINE_DRAW_H\n"
        "#define UNITY_PACK_ENGINE_DRAW_H\n"
        "\n"
        "typedef struct EngineDraw {\n"
        "    float x, y, half_w, half_h;\n"
        "    float m00, m01, m10, m11; /* local XY → world XY (full quat) */\n"
        "    float r, g, b;\n"
        "    float a; /* tint alpha */\n"
        "    int tex; /* engine_texture_* index; -1 if none */\n"
        "    int sorting_layer; /* TagManager m_SortingLayers index */\n"
        "    int sorting_order; /* SpriteRenderer.m_SortingOrder */\n"
        "    int flags; /* 1: lit by the 2D lights (URP Sprite-Lit-Default) */\n"
        "    int go; /* its GameObject (-1: unknown): the 2D effects' table */\n"
        "    float z; /* world z: a tie in layer and order draws the farther first */\n"
        "    int layer; /* its GameObject's layer: the cameras' culling masks; -1 Screen\n"
        "                  Space Overlay UI (after every camera), -2 Screen Space Camera UI */\n"
        "} EngineDraw;\n"
        "\n"
        + _ENGINE_CAMERA_TYPEDEF +
        "/* The cameras that render this frame, lowest depth first, and last an\n"
        " * overlay pass (Screen Space Overlay UI) when there is one; 0: no camera\n"
        " * (draw the Camera_main_* view). engine_draw_camera(k) makes the\n"
        " * Camera_main_* globals entry k's view and the next engine_collect_draws\n"
        " * what it sees; -1 restores the main camera's (with the overlay UI). */\n"
        "int engine_collect_cameras(EngineCamera *out, int max);\n"
        "void engine_draw_camera(int k);\n"
        + _ENGINE_TRI_TYPEDEF + "\n"
        "/* MeshFilter / MeshRenderer triangles, world space, active only */\n"
        "int engine_collect_tris(EngineTri *out, int max);\n"
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
        "/* Host pointer for uGUI Button (screen px, origin bottom-left). */\n"
        "extern float engine_pointer_x;\n"
        "extern float engine_pointer_y;\n"
        "extern int engine_pointer_down;\n"
        "/* Player Settings defaultScreenWidth/Height → Screen.* */\n"
        "extern int Screen_width;\n"
        "extern int Screen_height;\n"
        "extern int Screen_fullScreen; /* fullscreenMode 0/1 */\n"
        "extern int Screen_fullScreenNative; /* defaultIsNativeResolution */\n"
        "extern int Screen_maximized; /* fullscreenMode MaximizedWindow */\n"
        "extern const char engine_product_name[]; /* productName */\n"
        "/* Unity -logFile: default platform Player.log; \"-\" = stdout. */\n"
        "void engine_set_log_file(const char *path);\n"
        "void engine_apply_argv(int argc, char **argv);\n"
        "const char *engine_console_log_path(void); /* Application.consoleLogPath */\n"
        "const char *engine_data_path(void); /* Application.dataPath */\n"
        "const char *engine_persistent_data_path(void); "
        "/* Application.persistentDataPath */\n"
        "int engine_wants_quit(void); /* Application.Quit requested */\n"
        "\n"
        "#endif\n"
    )


def main():
    args = list(sys.argv[1:])
    outdir = None
    if "-o" in args:
        i = args.index("-o")
        if i + 1 >= len(args):
            sys.stderr.write("unity_pack: -o needs a directory\n")
            return 2
        outdir = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1 or args[0].startswith("-"):
        sys.stderr.write(
            "usage: unity_pack.py <project-dir> [-o <out-dir>]\n"
            "  default out-dir: $TMPDIR/<project folder>\n"
            "  needs CCSharp beside this repo (or $CCSHARP_HOME)\n")
        return 2
    import tools.unity.unity_pack_ccs as ccs
    if not ccs.available():
        sys.stderr.write("unity_pack: needs CCSharp beside this repo (or $CCSHARP_HOME)\n")
        return 1
    root = args[0]
    outdir = outdir or default_pack_dir(root)
    try:
        assets = _asset_guid_map(root)
        objs, _lights, cams, hier = _load_scenes_lights_cameras(root, assets)
        objs = ccs.scene_records(objs, hier)
        prefabs = ccs.collect_prefabs(root, objs, assets)
    except PackError as e:
        sys.stderr.write("unity_pack: %s\n" % e.message)
        return 1
    ok, why = ccs.eligible(objs, cams, prefabs)
    if not ok:
        sys.stderr.write("unity_pack: CCSharp does not cover this project: %s\n" % why)
        return 1
    os.makedirs(outdir, exist_ok=True)
    scripts = [p for p in _walk_files(os.path.join(root, "Assets"), (".cs",))
               if "Editor" not in p.split(os.sep)]
    scripts += ccs.plugin_sources([p for p in _walk_files(os.path.join(root, "Assets"), (".dll",))
                                   if "Editor" not in p.split(os.sep)], outdir)
    company, product = ccs.project_identity(root)
    project = {"company": company, "product": product,
               "scenes": [s["name"] for s in getattr(_load_scenes_lights_cameras, "scenes", [])],
               "layer_names": read_layer_names(root), "layer_matrix": read_layer_matrix(root), "assets": assets}
    return ccs.build(objs, cams, outdir, prefabs=prefabs, materials=_load_physics_materials(assets)[0], scripts=scripts, project=project)


if __name__ == "__main__":
    sys.exit(main())
