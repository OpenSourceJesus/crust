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
    '_parse_vec3_keyframes_full',
    '_resolve_anim_child_path',
]


_NUM = r"(-?Infinity|[-0-9.eE+]+)"
_VEC = r"\{x:\s*%s,\s*y:\s*%s,\s*z:\s*%s\}" % (_NUM, _NUM, _NUM)


def _num(v):
    v = v.strip()
    if v in ("Infinity", "-Infinity"):
        # a constant ("stepped") key: held until the next one
        return 1e30 if v[0] != "-" else -1e30
    return float(v)


def _parse_vec3_keyframes_full(curve_text):
    """(t, x, y, z, in x, y, z, out x, y, z) keys of a Vector3 curve, with
    their Hermite tangents (`inSlope` / `outSlope`; an infinite slope, Unity's
    "constant" key, is +-1e30). A key without slopes gets the straight line's
    (the secants to its neighbours), so it is sampled linearly as before."""
    keys = []
    for m in re.finditer(r"time:\s*%s\s*\n\s*value:\s*%s" % (_NUM, _VEC),
                         curve_text):
        rest = curve_text[m.end():]
        nxt = re.search(r"\btime:", rest)
        chunk = rest[:nxt.start()] if nxt else rest
        ins = re.search(r"inSlope:\s*" + _VEC, chunk)
        outs = re.search(r"outSlope:\s*" + _VEC, chunk)
        keys.append([_num(m.group(1))] + [_num(m.group(k)) for k in (2, 3, 4)]
                    + ([_num(ins.group(k)) for k in (1, 2, 3)] if ins else [None] * 3)
                    + ([_num(outs.group(k)) for k in (1, 2, 3)] if outs else [None] * 3))
    keys.sort(key=lambda k: k[0])
    for i, k in enumerate(keys):
        for a in range(3):
            if k[4 + a] is None:        # in: the secant from the key before
                if i > 0 and k[0] > keys[i - 1][0]:
                    k[4 + a] = (k[1 + a] - keys[i - 1][1 + a]) / (k[0] - keys[i - 1][0])
                else:
                    k[4 + a] = 0.0
            if k[7 + a] is None:        # out: the secant to the key after
                if i + 1 < len(keys) and keys[i + 1][0] > k[0]:
                    k[7 + a] = (keys[i + 1][1 + a] - k[1 + a]) / (keys[i + 1][0] - k[0])
                else:
                    k[7 + a] = 0.0
    return [tuple(k) for k in keys]


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

    def _vec3_tracks(section_name):
        """Every curve of a Vector3 section: its path and full keys."""
        out = []
        sm = re.search(
            r"(?ms)^  %s:\s*\n(.*?)(?=^  m_[A-Z]|\Z)" % section_name, text)
        if not sm or sm.group(1).strip().startswith("[]"):
            return out
        for cm in re.finditer(
                r"(?ms)^  - curve:\n(.*?)(?=^  - curve:|^  m_|\Z)", sm.group(1)):
            block = cm.group(1)
            pm = re.search(r"(?m)^\s+path:\s*(.*)$", block)
            path = pm.group(1) if pm else ""
            keys = _parse_vec3_keyframes_full(block)
            if keys:
                out.append({"path": "" if _curve_path_is_root(path)
                            else path.strip().strip('"'), "keys": keys})
        return out

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
    # every Vector3 curve, the root's and its children's: 0 position,
    # 1 rotation (Euler degrees), 2 scale
    tracks = []
    for prop, sec in ((0, "m_PositionCurves"), (1, "m_EulerCurves"),
                      (2, "m_ScaleCurves")):
        for tr in _vec3_tracks(sec):
            tracks.append(dict(tr, prop=prop))
    pos_full = [k for tr in tracks if tr["prop"] == 0 and not tr["path"]
                for k in tr["keys"]]
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
        "pos_full": pos_full,
        "tracks": tracks,
        "events": _parse_animation_events(text),
        "sprite_curves": sprite_curves,
    }


def _parse_animation_events(text):
    """m_Events: [{time, function, float, int, string}] (Animation Events)."""
    sm = re.search(r"(?ms)^  m_Events:\s*\n(.*?)(?=^  m_[A-Z]|\Z)", text)
    if not sm or sm.group(1).strip().startswith("[]"):
        return []
    out = []
    for em in re.finditer(r"(?ms)^  - time:\s*([0-9.eE+-]+)\s*\n(.*?)(?=^  - time:|\Z)",
                          sm.group(1)):
        body = em.group(2)

        def f(key, conv, default):
            m = re.search(r"(?m)^\s+%s:[ \t]*(.*)$" % key, body)
            try:
                return conv(m.group(1).strip()) if m else default
            except ValueError:
                return default
        fn = f("functionName", str, "")
        if fn:
            out.append({"time": float(em.group(1)), "function": fn,
                        "float": f("floatParameter", float, 0.0),
                        "int": f("intParameter", int, 0),
                        "string": f("data", lambda v: v.strip("'\""), "")})
    return sorted(out, key=lambda e: e["time"])


def _script_method_args(cl, name):
    """The parameter list of the class's one instance method `name` (from
    its script, as the pack reads it), or None: none, or overloaded."""
    try:
        src = cs2cpp._blank(_read(cl.get("script_path") or ""))
    except (IOError, OSError):
        return None
    hits = re.findall(r"(?<![\w.])(?!static\b)(?:void|IEnumerator)\s+%s\s*\(([^()]*)\)"
                      % re.escape(name), src)
    if len(hits) != 1 or re.search(r"\bstatic\s+void\s+%s\s*\(" % re.escape(name), src):
        return None
    return hits[0].strip()


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


def _fill_linear_slopes(keys, clip_list):
    """Root position keys without tangents (a key tuple of four): the
    secants, so they are sampled linearly as before."""
    for c in clip_list:
        b, n = c["key_begin"], c["key_count"]
        for i in range(b, b + n):
            k = keys[i]
            if "ix" in k:
                continue
            for a in "xyz":
                prev = keys[i - 1] if i > b else None
                nxt = keys[i + 1] if i + 1 < b + n else None
                k["i" + a] = ((k[a] - prev[a]) / (k["t"] - prev["t"])
                              if prev and k["t"] > prev["t"] else 0.0)
                k["o" + a] = ((nxt[a] - k[a]) / (nxt["t"] - k["t"])
                              if nxt and nxt["t"] > k["t"] else 0.0)


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
            "pos_full": list(clip.get("pos_full") or []),
            "tracks": list(clip.get("tracks") or []),
            "events": list(clip.get("events") or []),
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

    def key_row(k):
        t, x, y, z = k[:4]
        row = {"t": t, "x": x, "y": y, "z": z}
        if len(k) >= 10:
            row.update(ix=k[4], iy=k[5], iz=k[6], ox=k[7], oy=k[8], oz=k[9])
        return row
    keys = []
    for c in clip_list:
        c["key_begin"] = len(keys)
        full = c.get("pos_full") or []
        src = full if len(full) == len(c["pos_keys"]) else c["pos_keys"]
        for k in src:
            keys.append(key_row(k))
        c["key_count"] = len(c["pos_keys"])
    _fill_linear_slopes(keys, clip_list)
    # Tracks (every curve but the root's position, which keeps its own
    # path) and each player's bindings of them to their targets: its own
    # Transform, or the child the curve's path names.
    tkeys, tracks = [], []
    for c in clip_list:
        c["track_ids"] = []
        for tr in c.get("tracks") or []:
            if tr["prop"] == 0 and not tr["path"]:
                continue
            c["track_ids"].append(len(tracks))
            tracks.append({"begin": len(tkeys), "count": len(tr["keys"]),
                           "prop": tr["prop"], "path": tr["path"]})
            tkeys.extend(key_row(k) for k in tr["keys"])
    from tools.unity_pack import _class_has_position  # (imports this module)
    binds = []
    rot_cls, scale_cls = set(), set()
    for pl in players:
        pl["bind_begin"] = len(binds)
        owner = pl.get("owner_obj")
        for ti in clip_list[pl["clip"]].get("track_ids") or []:
            tr = tracks[ti]
            if tr["path"]:
                hit = _resolve_anim_child_path(owner, tr["path"], plan)
                if not hit:
                    # Not silently: a path through an object with nothing
                    # but a Transform (not packed as an instance) does not
                    # resolve, and the curve was dropped without a word.
                    sys.stderr.write(
                        "unity_pack: warning: animation curve on %r of %r is not "
                        "played: the path does not reach a packed object (an "
                        "object with only a Transform on the way is not packed)\n"
                        % (tr["path"], (owner or {}).get("name")))
                    continue
                tc, tinst, _to = hit
            else:
                tc, tinst = pl["owner_class"], pl["owner_inst"]
            tcl = plan["classes"].get(tc) or {}
            if tr["prop"] == 0 and (tcl.get("static")
                                    or not _class_has_position(tcl)):
                sys.stderr.write(
                    "unity_pack: warning: animated position of %r (%s) is not "
                    "moved: its class is packed static\n" % (tr["path"], tc))
                continue
            (rot_cls if tr["prop"] == 1 else scale_cls if tr["prop"] == 2
             else set()).add(tc)
            binds.append({"track": ti, "prop": tr["prop"],
                          "target_class": tc,
                          "target_class_id": int(class_ids[tc]),
                          "target_inst": int(tinst)})
        pl["bind_count"] = len(binds) - pl["bind_begin"]
    # Animation Events: the GameObject's scripts with a method of that name
    # (SendMessage), called with the event's parameter when it takes one
    by_go = {}
    for cname, cl in plan["classes"].items():
        for k, o in enumerate(cl.get("instances") or []):
            if o.get("go_index") is not None:
                by_go.setdefault(int(o["go_index"]), []).append((cname, k))
    methods_by = plan.get("_methods_by") or {}
    for pl in players:
        pl["events"] = []
        owner = pl.get("owner_obj") or {}
        targets = by_go.get(owner.get("go_index"), []) \
            if owner.get("go_index") is not None else \
            [(pl["owner_class"], pl["owner_inst"])]
        for ev in clip_list[pl["clip"]].get("events") or []:
            for cname, k in targets:
                args = _script_method_args(plan["classes"][cname], ev["function"])
                if args is None:
                    continue
                pl["events"].append(dict(ev, cls=cname,
                                         class_id=int(class_ids[cname]),
                                         inst=int(k), args=args))
    plan["anim_event_methods"] = {}
    for pl in players:
        for e in pl.get("events") or []:
            plan["anim_event_methods"].setdefault(e["cls"], set()).add(e["function"])
    # the Transforms they turn / scale keep live rotation / scale tables
    plan["anim_rot_classes"] = sorted(rot_cls)
    plan["anim_scale_classes"] = sorted(scale_cls)

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
        "track_keys": tkeys,
        "tracks": tracks,
        "binds": binds,
        "players": players,
        "sprite_keys": sprite_keys,
        "sprite_binds": sprite_binds,
    }
