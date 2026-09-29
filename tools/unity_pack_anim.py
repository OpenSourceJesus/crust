# SPDX-License-Identifier: MIT
"""unity_pack: Animation: AnimationClip and AnimatorController parsing, keyframes and
sprite curves, and the packed animation tables.

Moved out of tools/unity_pack.py unchanged; unity_pack re-exports every name
here, so `unity_pack.<name>` keeps working."""

from __future__ import annotations
import hashlib
import json
import os
import pickle
import re
import sys
import math
import copy
import tools.cs2cpp as cs2cpp  # noqa: E402

import tools.cs2cpp as cs2cpp  # noqa: E402
from tools.unity_pack_common import *  # noqa: E402,F401,F403
from tools.unity_pack_physics import *  # noqa: E402,F401,F403
from tools.unity_pack_sprites import *  # noqa: E402,F401,F403
from tools.unity_pack_ui import *  # noqa: E402,F401,F403

__all__ = [
    '_anim_sprite_guids',
    '_build_animation_tables',
    '_curve_path_is_root',
    '_load_animation_assets',
    '_parse_animation_clip',
    '_parse_animator_controller_default_clip',
    '_parse_float_keyframes',
    '_parse_pptr_sprite_curves',
    '_parse_vec3_keyframes',
    '_resolve_anim_child_path',
]


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


def _parse_pptr_sprite_curves(text):
    """Authored m_PPtrCurves with attribute m_Sprite → path + (t, guid) keys."""
    out = []
    sm = re.search(
        r"(?ms)^  m_PPtrCurves:\s*\n(.*?)(?=^  m_[A-Z]|\Z)", text)
    if not sm:
        return out
    body = sm.group(1)
    if body.strip().startswith("[]"):
        return out
    for cm in re.finditer(
            r"(?ms)^  - serializedVersion:.*?"
            r"(?=^  - serializedVersion:|^  m_|\Z)", body):
        block = cm.group(0)
        attr = re.search(r"(?m)^\s+attribute:\s*(.+)$", block)
        if not attr or attr.group(1).strip() != "m_Sprite":
            continue
        path_m = re.search(r"(?m)^\s+path:\s*(.*)$", block)
        path = path_m.group(1).strip().strip('"') if path_m else ""
        keys = []
        for km in re.finditer(
                r"(?m)^\s+- time:\s*([0-9.eE+-]+)\s*\n"
                r"\s+value:\s*\{fileID:\s*-?\d+,\s*guid:\s*"
                r"([0-9a-fA-F]+)",
                block):
            keys.append((float(km.group(1)), km.group(2).lower()))
        if keys:
            out.append({"path": path, "keys": keys})
    return out


def _parse_animation_clip(text):
    """Authored .anim → length, loop, legacy, root position/euler/scale keys."""
    nm = re.search(r"(?m)^\s+m_Name:\s*(.+)$", text)
    stop = re.search(r"(?m)^\s+m_StopTime:\s*([0-9.eE+-]+)", text)
    loop = re.search(r"(?m)^\s+m_LoopTime:\s*(\d+)", text)
    wrap = re.search(r"(?m)^\s+m_WrapMode:\s*(\d+)", text)
    legacy = re.search(r"(?m)^\s+m_Legacy:\s*(\d+)", text)
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
    sprite_curves = _parse_pptr_sprite_curves(text)
    length = float(stop.group(1)) if stop else 0.0
    if length <= 0.0:
        for keys in (pos, euler, scale):
            if keys:
                length = max(length, keys[-1][0])
        for sc in sprite_curves:
            if sc["keys"]:
                length = max(length, sc["keys"][-1][0])
        if length <= 0.0:
            length = 1.0
    return {
        "name": (nm.group(1).strip() if nm else "Clip"),
        "length": length,
        "loop": do_loop,
        "legacy": int(legacy.group(1)) if legacy else 0,
        "pos_keys": pos,
        "euler_keys": euler,
        "scale_keys": scale,
        "sprite_curves": sprite_curves,
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


def _anim_sprite_guids(objects):
    """All sprite PNG guids referenced by authored AnimationClip PPtr curves."""
    out = []
    for o in objects or []:
        p = o.get("anim_player")
        if not p or not p.get("clip"):
            continue
        for sc in p["clip"].get("sprite_curves") or []:
            for _t, g in sc.get("keys") or []:
                if g:
                    out.append(g)
    return out


def _resolve_anim_child_path(owner, path, plan):
    """Unity curve path under Animator owner → (class, inst, obj) or None."""
    path = (path or "").strip().strip('"')
    index = {}
    for cname, cl in plan["classes"].items():
        for i, o in enumerate(cl.get("instances") or []):
            fid = str(o.get("father_id") or "0")
            index.setdefault((fid, o.get("name")), []).append((cname, i, o))
    if not path:
        return None
    cur_xf = str(owner.get("xf_id") or "0")
    hit = None
    for part in path.split("/"):
        kids = index.get((cur_xf, part))
        if not kids:
            return None
        hit = kids[0]
        cur_xf = str(hit[2].get("xf_id") or "0")
    return hit


def _build_animation_tables(plan):
    """Authored Animation / Animator players + shared clip keyframe tables.

    Root position curves write absolute Transform.localPosition values from
    the clip (Bob.anim x=2 → Spinner/Wave at x=2). Legacy clips bind only to
    Animation; Mecanim clips only to Animator — see parse_unity_yaml.

    m_PPtrCurves attribute m_Sprite swap SpriteRenderer.tex on the path child
    (Idle.anim → Graphics).
    """
    clips_by_guid = {}
    clip_list = []
    players = []
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    textures = plan.get("textures") or []
    guid_to_tex = {t["guid"]: i for i, t in enumerate(textures)}

    def _ensure_clip(guid, clip):
        if guid in clips_by_guid:
            return clips_by_guid[guid]
        # Empty m_PositionCurves → no root motion; do not invent (0,0,0) keys
        # (that teleports the Transform every tick — Idle.anim is sprite-only).
        pos = list(clip.get("pos_keys") or [])
        idx = len(clip_list)
        entry = {
            "guid": guid,
            "name": clip.get("name") or "Clip",
            "length": float(clip.get("length") or 1.0),
            "loop": int(clip.get("loop") or 0),
            "legacy": int(clip.get("legacy") or 0),
            "pos_keys": pos,
            # rotation (Euler degrees, sampled in Euler space as Unity's Euler
            # curves are) and scale: they were parsed, then dropped
            "euler_keys": list(clip.get("euler_keys") or []),
            "scale_keys": list(clip.get("scale_keys") or []),
            "sprite_curves": list(clip.get("sprite_curves") or []),
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
            rest = o.get("local_pos") or o.get("pos") or (0.0, 0.0, 0.0)
            players.append({
                "name": o.get("name") or "obj",
                "owner_class": cname,
                "owner_class_id": cid,
                "owner_inst": i,
                "owner_obj": o,
                "clip": ci,
                "playing": int(p.get("playing") or 0),
                # an authored 0 is a paused player, not the default
                "speed": float(p["speed"] if p.get("speed") is not None else 1.0),
                "loop": int(p.get("loop")
                            if p.get("loop") is not None
                            else clip_list[ci]["loop"]),
                # Rest kept for diagnostics; tick writes absolute curve samples.
                "rest_x": float(rest[0]),
                "rest_y": float(rest[1]),
                "rest_z": float(rest[2]) if len(rest) > 2 else 0.0,
                "kind": 0 if p.get("kind") == "animation" else 1,
                "sprite_bind_begin": 0,
                "sprite_bind_count": 0,
            })

    keys = []
    for c in clip_list:
        c["key_begin"] = len(keys)
        for t, x, y, z in c["pos_keys"]:
            keys.append({"t": t, "x": x, "y": y, "z": z})
        c["key_count"] = len(c["pos_keys"])
    rot_keys, scale_keys = [], []
    for c in clip_list:
        for field, table, name in (("euler_keys", rot_keys, "rkey"),
                                   ("scale_keys", scale_keys, "skey")):
            c[name + "_begin"] = len(table)
            for t, x, y, z in c[field]:
                table.append({"t": t, "x": x, "y": y, "z": z})
            c[name + "_count"] = len(c[field])
    # the owners they turn / scale keep live rotation / scale tables
    plan["anim_rot_classes"] = sorted({pl["owner_class"] for pl in players
                                       if clip_list[pl["clip"]]["euler_keys"]})
    plan["anim_scale_classes"] = sorted({pl["owner_class"] for pl in players
                                         if clip_list[pl["clip"]]["scale_keys"]})

    sprite_keys = []
    sprite_binds = []
    mutable = set()
    for pl in players:
        clip = clip_list[pl["clip"]]
        curves = clip.get("sprite_curves") or []
        if not curves:
            pl.pop("owner_obj", None)
            continue
        owner = pl.get("owner_obj")
        begin = len(sprite_binds)
        for sc in curves:
            path = (sc.get("path") or "").strip().strip('"')
            hit = _resolve_anim_child_path(owner, path, plan)
            if hit:
                tc, ti, to = hit
            elif not path:
                tc, ti, to = (
                    pl["owner_class"], pl["owner_inst"], owner)
            else:
                continue
            if not to:
                continue
            sp = to.get("sprite") or {}
            ls = to.get("local_scale") or (1.0, 1.0, 1.0)
            sx = abs(float(sp.get("scale_x", ls[0])))
            sy = abs(float(sp.get("scale_y", ls[1])))
            skb = len(sprite_keys)
            for t, g in sc.get("keys") or []:
                tid = guid_to_tex.get(g)
                if tid is None:
                    continue
                tex = textures[tid]
                ppu = float(tex.get("ppu") or 100.0)
                if ppu <= 0.0:
                    ppu = 100.0
                hw = (float(tex["w"]) / ppu) * sx * 0.5
                hh = (float(tex["h"]) / ppu) * sy * 0.5
                sprite_keys.append({
                    "t": float(t), "tex": int(tid),
                    "hw": hw, "hh": hh,
                })
            skc = len(sprite_keys) - skb
            if skc <= 0:
                continue
            sprite_binds.append({
                "target_class": tc,
                "target_class_id": int(class_ids[tc]),
                "target_inst": int(ti),
                "key_begin": skb,
                "key_count": skc,
            })
            mutable.add(tc)
        pl["sprite_bind_begin"] = begin
        pl["sprite_bind_count"] = len(sprite_binds) - begin
        pl.pop("owner_obj", None)

    plan["sprite_draw_mutable"] = sorted(mutable)
    return {
        "clips": clip_list,
        "keys": keys,
        "rot_keys": rot_keys,
        "scale_keys": scale_keys,
        "players": players,
        "sprite_keys": sprite_keys,
        "sprite_binds": sprite_binds,
    }
