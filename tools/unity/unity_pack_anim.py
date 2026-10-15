# SPDX-License-Identifier: MIT
"""unity_pack: Animation: AnimationClip and AnimatorController parsing, keyframes and
sprite curves, and the packed animation tables.

Moved out of tools/unity/unity_pack.py unchanged; unity_pack re-exports every name
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
from tools.unity.unity_pack_common import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_physics import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_sprites import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_ui import *  # noqa: E402,F401,F403

__all__ = [
    '_anim_sprite_guids',
    '_animator_sprite_guids',
    '_build_animator_tables',
    '_emit_animator_protos',
    '_emit_animator_runtime',
    '_parse_animator_machine',
    '_parse_float_curves',
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
        path_m = re.search(r"(?m)^\s+path:[ \t]*(.*)$", block)
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
            pm = re.search(r"(?m)^\s+path:[ \t]*(.*)$", block)
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
            pm = re.search(r"(?m)^\s+path:[ \t]*(.*)$", block)
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
    from tools.unity.unity_pack import _class_has_position  # (imports this module)
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
            skb = len(sprite_keys)
            sprite_keys.extend(_sprite_key_rows(sc.get("keys") or [], to, plan))
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

    plan["sprite_draw_mutable"] = sorted(
        mutable | set(plan.get("sprite_draw_mutable") or ()))
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


# ---- AnimatorController state machines (Animator.Play / IsName) -----------

def _parse_float_curves(text):
    """m_FloatCurves: ``[{attr, path, class_id, keys: [(t, v, in, out)],
    weighted}]`` -- a component's float property (`multiplySize.x` of a
    script, classID 114) over time."""
    sm = re.search(r"(?ms)^  m_FloatCurves:\s*\n(.*?)(?=^  m_[A-Z]|\Z)", text)
    if not sm or sm.group(1).strip().startswith("[]"):
        return []
    out = []
    key = re.compile(r"time:\s*%s\s*\n\s*value:\s*%s\s*\n\s*inSlope:\s*%s\s*"
                     r"\n\s*outSlope:\s*%s" % ((_NUM,) * 4))
    for cm in re.finditer(r"(?ms)^  - serializedVersion:.*?"
                          r"(?=^  - serializedVersion:|\Z)", sm.group(1)):
        b = cm.group(0)
        attr = re.search(r"(?m)^    attribute:[ \t]*(.*)$", b)
        path = re.search(r"(?m)^    path:[ \t]*(.*)$", b)
        cid = re.search(r"(?m)^    classID:\s*(\d+)", b)
        out.append({
            "attr": attr.group(1).strip() if attr else "",
            "path": path.group(1).strip().strip('"') if path else "",
            "class_id": int(cid.group(1)) if cid else 0,
            "keys": [tuple(_num(k.group(j)) for j in (1, 2, 3, 4))
                     for k in key.finditer(b)],
            "weighted": bool(re.search(r"weightedMode:\s*[1-3]", b)),
        })
    return out


def _parse_animator_machine(text):
    """An AnimatorController's layers as ``({"layers": [{name, weight,
    default, states: [{name, speed, clip_guid, exit: (dst, exit_time) or
    None}]}]}, None)``, or ``(None, why)`` for what this does not model:
    parameters and conditions, blend durations and offsets, sub-state
    machines, Any State transitions, blend trees, masks, synced or
    additive layers, partial weights."""
    docs = {m.group(2): (m.group(1), m.group(3)) for m in re.finditer(
        r"(?ms)^--- !u!(\d+) &(-?\d+)[^\n]*\n(.*?)(?=^--- |\Z)", text)}
    ctrl = next((b for c, b in docs.values() if c == "91"), None)
    if ctrl is None:
        return None, "no AnimatorController"
    if re.search(r"m_AnimatorParameters:\s*\n\s*- ", ctrl):
        return None, "parameters"

    def ids(body, key):
        m = re.search(r"(?ms)^  %s:[ \t]*(\[\])?\n?((?:  - [^\n]*\n(?:    "
                      r"[^\n]*\n)*)*)" % key, body)
        return [] if not m or m.group(1) else re.findall(
            r"fileID:\s*(-?\d+)", m.group(2))

    def num(body, key, dflt=0.0):
        m = re.search(r"(?m)^\s+%s:\s*(-?[0-9.eE+-]+)" % key, body)
        return float(m.group(1)) if m else dflt

    layers = []
    for k, lm in enumerate(re.finditer(
            r"(?ms)^  - serializedVersion: \d+\n    m_Name: ([^\n]*)\n"
            r"(.*?)(?=^  - serializedVersion|^  m_\w|\Z)", ctrl)):
        lname, lb = lm.group(1).strip(), lm.group(2)
        smid = re.search(r"m_StateMachine:\s*\{fileID:\s*(-?\d+)", lb)
        weight = 1.0 if k == 0 else num(lb, "m_DefaultWeight")
        if (re.search(r"m_Mask:\s*\{fileID:\s*[1-9-]", lb)
                or num(lb, "m_BlendingMode") or num(lb, "m_SyncedLayerIndex", -1)
                >= 0 or weight not in (0.0, 1.0) or not smid
                or smid.group(1) not in docs):
            return None, "layer %r (mask, additive, synced or partial weight)" \
                % lname
        sm = docs[smid.group(1)][1]
        # ponytail: Entry transitions are only taken when a machine is
        # re-entered through Exit, refused below; a layer starts in its
        # default state
        for key in ("m_ChildStateMachines", "m_AnyStateTransitions"):
            if ids(sm, key):
                return None, "%s in layer %r" % (key[2:], lname)
        sids = re.findall(r"m_State:\s*\{fileID:\s*(-?\d+)", sm)
        dflt = re.search(r"m_DefaultState:\s*\{fileID:\s*(-?\d+)", sm)
        states = []
        for sid in sids:
            sb = docs.get(sid, ("", ""))[1]
            name = re.search(r"(?m)^  m_Name:\s*(.*)$", sb)
            mo = re.search(r"m_Motion:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*"
                           r"([0-9a-fA-F]+))?", sb)
            if (num(sb, "m_SpeedParameterActive") or num(sb, "m_CycleOffset")
                    or (mo and mo.group(1) != "0" and not mo.group(2))):
                return None, "state %r (speed parameter, cycle offset or " \
                    "blend tree)" % (name.group(1) if name else sid)
            trs = [t for t in ids(sb, "m_Transitions")
                   if not num(docs.get(t, ("", ""))[1], "m_Mute")]
            if len(trs) > 1:
                return None, "state %r has more than one unmuted transition" % (
                    name.group(1).strip())
            exit_ = None
            for tid in trs:
                tb = docs.get(tid, ("", ""))[1]
                dst = re.search(r"m_DstState:\s*\{fileID:\s*(-?\d+)", tb)
                if (ids(tb, "m_Conditions") or num(tb, "m_TransitionDuration")
                        or num(tb, "m_TransitionOffset") or num(tb, "m_IsExit")
                        or not num(tb, "m_HasExitTime") or not dst
                        or dst.group(1) not in sids):
                    return None, "a transition of state %r (conditions, " \
                        "duration or exit)" % name.group(1).strip()
                exit_ = (sids.index(dst.group(1)), num(tb, "m_ExitTime", 1.0))
            states.append({
                "name": name.group(1).strip() if name else "",
                "speed": num(sb, "m_Speed", 1.0),
                "clip_guid": (mo.group(2).lower()
                              if mo and mo.group(2) else None),
                "exit": exit_})
        if not dflt or dflt.group(1) not in sids:
            return None, "layer %r has no default state" % lname
        layers.append({"name": lname, "weight": weight, "states": states,
                       "default": sids.index(dflt.group(1))})
    return {"layers": layers}, None


_MACHINES = {}


def _animator_machine(guid, asset_guids):
    """(machine, why) of a controller asset, its states' clips parsed."""
    key = (guid, (asset_guids or {}).get(guid))
    if key not in _MACHINES:
        path = key[1]
        try:
            m, why = _parse_animator_machine(_read(path)) if path else (
                None, "controller asset not found")
        except (IOError, OSError):
            m, why = None, "controller asset not readable"
        for lay in (m or {}).get("layers") or []:
            for s in lay["states"]:
                cp = (asset_guids or {}).get(s["clip_guid"] or "")
                if cp:
                    text = _read(cp)
                    s["clip"] = dict(_parse_animation_clip(text),
                                     float_curves=_parse_float_curves(text))
        _MACHINES[key] = (m, why)
    return _MACHINES[key]


def _animator_sprite_guids(objects, asset_guids):
    """The sprites the state machines' clips swap in (texture loading)."""
    out = []
    for o in objects or []:
        m, _w = _animator_machine(o.get("animator_controller"), asset_guids) \
            if o.get("animator_controller") else (None, None)
        for lay in (m or {}).get("layers") or []:
            for s in lay["states"]:
                for sc in (s.get("clip") or {}).get("sprite_curves") or []:
                    out.extend(g for _t, g in sc["keys"] if g)
    return out


def _sprite_key_rows(keys, to, plan):
    """``[{t, tex, hw, hh}]`` of a sprite curve drawn on object record *to*."""
    textures = plan.get("textures") or []
    guid_to_tex = {t["guid"]: i for i, t in enumerate(textures)}
    sp = to.get("sprite") or {}
    ls = to.get("local_scale") or (1.0, 1.0, 1.0)
    sx = abs(float(sp.get("scale_x", ls[0])))
    sy = abs(float(sp.get("scale_y", ls[1])))
    rows = []
    for t, g in keys:
        tid = guid_to_tex.get(g)
        if tid is None:
            continue
        tex = textures[tid]
        ppu = float(tex.get("ppu") or 100.0)
        ppu = ppu if ppu > 0.0 else 100.0
        rows.append({"t": float(t), "tex": int(tid),
                     "hw": (float(tex["w"]) / ppu) * sx * 0.5,
                     "hh": (float(tex["h"]) / ppu) * sy * 0.5})
    return rows


def _build_animator_tables(plan, asset_guids, field_types):
    """The Animators scripts reach through an `Animator` field (stored as
    its GameObject), run as their controllers' state machines: per layer a
    current state and its time, `Play` applied at the next animator update
    (after Update), exit-time transitions, and the current states' clips
    written -- a script's float fields and SpriteRenderer sprites.
    *field_types*: {(class, field): C# type}.

    ponytail: Write Defaults is not modelled (a state leaves what it does
    not animate as it is), and an inactive GameObject's Animator keeps
    running (Unity's pauses, and restarts from the default states when
    re-enabled)."""
    gos = {g for (cn, fn), row in (plan.get("go_field_refs") or {}).items()
           if field_types.get((cn, fn)) == "Animator" for g in row if g >= 0}
    if not gos:
        return None
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    by_go = {}
    for cname, cl in sorted(plan["classes"].items()):
        for i, o in enumerate(cl.get("instances") or []):
            if o.get("go_index") is not None:
                by_go.setdefault(int(o["go_index"]), []).append((cname, i, o))
    out = {"animators": [], "layers": [], "states": [], "fbinds": [],
           "fkeys": [], "sbinds": [], "skeys": []}
    mutable = set(plan.get("sprite_draw_mutable") or ())
    for go in sorted(gos):
        own = next(((c, i, o) for c, i, o in by_go.get(go, [])
                    if o.get("animator_controller")), None)
        m, why = (_animator_machine(own[2]["animator_controller"],
                                    asset_guids) if own
                  else (None, "no enabled Animator with a controller"))
        a = {"go": go, "layer_begin": len(out["layers"]), "layer_count": 0,
             "why": why or "", "name": (own[2].get("name") if own else "")}
        out["animators"].append(a)
        if why:
            sys.stderr.write("unity_pack: warning: the Animator on GameObject"
                             " %d is not run (%s); Animator.Play / IsName on"
                             " it stop the player\n" % (go, why))
            continue
        for lay in m["layers"]:
            first = len(out["states"])
            out["layers"].append({"name": lay["name"], "first": first,
                                  "count": len(lay["states"]),
                                  "default": first + lay["default"],
                                  "applies": lay["weight"] > 0.0})
            for s in lay["states"]:
                clip = s.get("clip") or {}
                st = {"name": s["name"],
                      "full": "%s.%s" % (lay["name"], s["name"]),
                      "speed": s["speed"],
                      "length": float(clip.get("length") or 1.0),
                      "loop": int(clip.get("loop") or 0),
                      "exit": (first + s["exit"][0], s["exit"][1])
                      if s["exit"] else (-1, 0.0), "why": "",
                      "fb": len(out["fbinds"]), "sb": len(out["sbinds"])}
                if s["clip_guid"] and not clip:
                    st["why"] = "its clip is not found"
                if (clip.get("tracks") or clip.get("events")
                        or clip.get("pos_keys")):
                    st["why"] = "its clip moves a Transform or has events"
                for fc in clip.get("float_curves") or []:
                    mem = fc["attr"].replace(".", "_")
                    hit = None
                    if fc["class_id"] == 114 and not fc["path"]:
                        hit = next(((c, i) for c, i, _o in by_go[go] if any(
                            n == mem and k in ("f32", "f16")
                            for n, _t, _b, k in plan["classes"][c]["members"]
                        )), None)
                    if not hit or fc["weighted"]:
                        st["why"] = "its clip animates %s (class %d)" % (
                            fc["attr"], fc["class_id"])
                        continue
                    out["fbinds"].append({
                        "cls": hit[0], "inst": hit[1], "member": mem,
                        "kb": len(out["fkeys"]), "kc": len(fc["keys"])})
                    out["fkeys"].extend(fc["keys"])
                for sc in clip.get("sprite_curves") or []:
                    hit = (_resolve_anim_child_path(own[2], sc["path"], plan)
                           if sc["path"] else own)
                    rows = _sprite_key_rows(sc["keys"], hit[2], plan) \
                        if hit and hit[2].get("sprite") else []
                    if not rows:
                        st["why"] = "its sprite curve on %r is not drawn" % (
                            sc["path"])
                        continue
                    out["sbinds"].append({
                        "cls": hit[0], "cid": class_ids[hit[0]],
                        "inst": hit[1], "kb": len(out["skeys"]),
                        "kc": len(rows)})
                    out["skeys"].extend(rows)
                    mutable.add(hit[0])
                st["fc"] = len(out["fbinds"]) - st["fb"]
                st["sc"] = len(out["sbinds"]) - st["sb"]
                out["states"].append(st)
            a["layer_count"] += 1
    plan["sprite_draw_mutable"] = sorted(mutable)
    return out


def _emit_animator_protos(p, plan):
    """The runtime's prototypes; in a strict pack, an Animator or state it
    cannot run is refused here rather than stopping the player."""
    am = plan.get("animators")
    if am and plan.get("strict"):
        for a in am["animators"]:
            if a["why"]:
                raise PackError("the Animator on %s is not run by crust (%s)"
                                % (a["name"] or "GameObject %d" % a["go"],
                                   a["why"]))
        for st in am["states"]:
            if st["why"]:
                raise PackError("Animator state %s is not run by crust (%s)"
                                % (st["full"], st["why"]))
    if am:
        p("static void Animator_Play(int go, const char *name, int layer,"
          " float nt);")
        p("static int Animator_IsName(int go, int layer, const char *name);")
        p("static float Animator_speed(int go);")
        p("static void Animator_set_speed(int go, float v);")


def _emit_animator_runtime(p, plan):
    """The tables of `_build_animator_tables` and the runtime: `Play`,
    `IsName` and `engine_animator_tick` (Unity's animator update, after
    Update)."""
    am = plan.get("animators")
    if not am:
        return
    A, L, S = am["animators"], am["layers"], am["states"]
    FB, SB = am["fbinds"], am["sbinds"]

    def arr(ty, name, vals, fmt="%s"):
        vals = list(vals) or [0]
        p("static const %s %s[%d] = { %s };" % (
            ty, name, len(vals), ", ".join(fmt % v for v in vals)))

    def f(v):
        return "%sf" % repr(float(v)) if abs(v) < 1e29 else (
            "1e30f" if v > 0 else "-1e30f")
    p("/* AnimatorController state machines (tools/unity/unity_pack_anim.py) */")
    arr("int", "_Animr_go", [a["go"] for a in A])
    arr("int", "_Animr_lb", [a["layer_begin"] for a in A])
    arr("int", "_Animr_lc", [a["layer_count"] for a in A])
    arr("char *const", "_Animr_why", [_c_str(a["why"]) for a in A])
    arr("char *const", "_AnimrL_name", [_c_str(x["name"]) for x in L])
    arr("int", "_AnimrL_first", [x["first"] for x in L])
    arr("int", "_AnimrL_count", [x["count"] for x in L])
    arr("int", "_AnimrL_default", [x["default"] for x in L])
    arr("int", "_AnimrL_applies", [int(x["applies"]) for x in L])
    arr("char *const", "_AnimrS_name", [_c_str(s["name"]) for s in S])
    arr("char *const", "_AnimrS_full", [_c_str(s["full"]) for s in S])
    arr("char *const", "_AnimrS_why", [_c_str(s["why"]) for s in S])
    arr("float", "_AnimrS_speed", [f(s["speed"]) for s in S])
    arr("float", "_AnimrS_len", [f(s["length"]) for s in S])
    arr("int", "_AnimrS_loop", [s["loop"] for s in S])
    arr("int", "_AnimrS_exit", [s["exit"][0] for s in S])
    arr("float", "_AnimrS_exit_t", [f(s["exit"][1]) for s in S])
    for k in ("fb", "fc", "sb", "sc"):
        arr("int", "_AnimrS_" + k, [s[k] for s in S])
    fk = am["fkeys"] or [(0.0, 0.0, 0.0, 0.0)]
    for j, col in enumerate(("t", "v", "i", "o")):
        arr("float", "_AnimrFK_" + col, [f(k[j]) for k in fk])
    arr("int", "_AnimrFB_kb", [b["kb"] for b in FB])
    arr("int", "_AnimrFB_kc", [b["kc"] for b in FB])
    sk = am["skeys"] or [{"t": 0.0, "tex": 0, "hw": 0.0, "hh": 0.0}]
    arr("float", "_AnimrSK_t", [f(k["t"]) for k in sk])
    arr("int", "_AnimrSK_tex", [k["tex"] for k in sk])
    arr("float", "_AnimrSK_hw", [f(k["hw"]) for k in sk])
    arr("float", "_AnimrSK_hh", [f(k["hh"]) for k in sk])
    arr("int", "_AnimrSB_kb", [b["kb"] for b in SB])
    arr("int", "_AnimrSB_kc", [b["kc"] for b in SB])
    arr("int", "_AnimrL_anim", [a for a, x in enumerate(A)
                                for _ in range(x["layer_count"])])
    p("static float _Animr_speed[%d] = { %s };" % (
        max(1, len(A)), ", ".join("1.f" for _ in A) or "1.f"))
    nl = max(1, len(L))
    p("static int _AnimrL_state[%d] = { %s };" % (nl, ", ".join(
        str(x["default"]) for x in L) or "0"))
    p("static float _AnimrL_time[%d];" % nl)
    p("static int _AnimrL_pend[%d] = { %s };" % (nl, ", ".join(
        "-1" for _ in L) or "-1"))
    p("static float _AnimrL_pend_t[%d];" % nl)
    p("""static int _animr_streq(const char *a, const char *b) {
    while (*a && *a == *b) { a = a + 1; b = b + 1; }
    return *a == *b;
}
static int _animr_of(int go, const char *api) {
    int a;
    if (go < 0) {
        fprintf(stderr, "NullReferenceException: Object reference not set to"
                " an instance of an object (%s)\\n", api);
        exit(70);
    }
    for (a = 0; a < %(na)d; a = a + 1)
        if (_Animr_go[a] == go) {
            if (_Animr_why[a][0]) {
                fprintf(stderr, "%s: the Animator is not run by crust (%s);"
                        " stopping rather than skipping it\\n", api,
                        _Animr_why[a]);
                exit(70);
            }
            return a;
        }
    fprintf(stderr, "%s: no Animator run by crust on GameObject %d;"
            " stopping rather than skipping it\\n", api, go);
    exit(70);
    return -1;
}
static float _animr_hermite(int kb, int kc, float t) {
    int k;
    if (kc <= 0) return 0.f;
    if (kc == 1 || t <= _AnimrFK_t[kb]) return _AnimrFK_v[kb];
    for (k = kb; k < kb + kc - 1; k = k + 1) {
        float t0 = _AnimrFK_t[k], t1 = _AnimrFK_t[k + 1], dt = t1 - t0;
        if (t <= t1) {
            float m0 = _AnimrFK_o[k], m1 = _AnimrFK_i[k + 1], u, u2, u3;
            if (dt <= 0.f) return _AnimrFK_v[k + 1];
            if (m0 > 1e29f || m0 < -1e29f || m1 > 1e29f || m1 < -1e29f)
                return _AnimrFK_v[k];
            u = (t - t0) / dt; u2 = u * u; u3 = u2 * u;
            return (2.f * u3 - 3.f * u2 + 1.f) * _AnimrFK_v[k]
                + (u3 - 2.f * u2 + u) * dt * m0
                + (3.f * u2 - 2.f * u3) * _AnimrFK_v[k + 1]
                + (u3 - u2) * dt * m1;
        }
    }
    return _AnimrFK_v[kb + kc - 1];
}
/* Animator.Play: the state, by its name or "Layer.State", in *layer* (-1:
   the first layer that has it), from normalized time *nt* (-Infinity: from
   the start, unless it is the state playing); applied at the animator
   update. A name no layer has is Unity's warning, and nothing. */
static void Animator_Play(int go, const char *name, int layer, float nt) {
    int a = _animr_of(go, "Animator.Play"), l, s;
    for (l = 0; l < _Animr_lc[a]; l = l + 1) {
        int k = _Animr_lb[a] + l;
        if (layer >= 0 && l != layer) continue;
        for (s = _AnimrL_first[k]; s < _AnimrL_first[k] + _AnimrL_count[k];
             s = s + 1) {
            if (!_animr_streq(_AnimrS_name[s], name)
                && !_animr_streq(_AnimrS_full[s], name)) continue;
            if (nt < -1e30f) {
                int cur = _AnimrL_pend[k] >= 0 ? _AnimrL_pend[k]
                                               : _AnimrL_state[k];
                if (cur == s) return;
                nt = 0.f;
            }
            _AnimrL_pend[k] = s;
            _AnimrL_pend_t[k] = nt * _AnimrS_len[s];
            return;
        }
    }
    fprintf(stderr, "Animator.GotoState: State could not be found\\n");
}
/* GetCurrentAnimatorStateInfo(layer).IsName(name) */
static int Animator_IsName(int go, int layer, const char *name) {
    int a = _animr_of(go, "AnimatorStateInfo.IsName"), s;
    if (layer < 0 || layer >= _Animr_lc[a]) return 0;
    s = _AnimrL_state[_Animr_lb[a] + layer];
    return _animr_streq(_AnimrS_name[s], name)
        || _animr_streq(_AnimrS_full[s], name);
}
static void _animr_enter(int k, int s, float t) {
    if (_AnimrS_why[s][0]) {
        fprintf(stderr, "Animator state %%s is not run by crust (%%s);"
                " stopping rather than skipping it\\n", _AnimrS_full[s],
                _AnimrS_why[s]);
        exit(70);
    }
    _AnimrL_state[k] = s;
    _AnimrL_time[k] = t;
}
/* Animator.speed: scales every layer's clock */
static float Animator_speed(int go) {
    return _Animr_speed[_animr_of(go, "Animator.speed")];
}
static void Animator_set_speed(int go, float v) {
    _Animr_speed[_animr_of(go, "Animator.speed")] = v;
}""".replace("%(na)d", str(len(A))).replace("%%", "%"))
    p("static void _animr_write_float(int b, float v) {")
    p("    switch (b) {")
    for j, b in enumerate(FB):
        p("    case %d: %s_set_%s(%du, v); break;" % (
            j, _c_ident(b["cls"]), b["member"], b["inst"]))
    p("    default: (void)v; break;")
    p("    }")
    p("}")
    p("static void _animr_write_sprite(int b, int tex, float hw, float hh) {")
    p("    switch (b) {")
    for j, b in enumerate(SB):
        idn = _c_ident(b["cls"])
        p("    case %d: _%s_draw_tex[%d] = tex; _%s_draw_hw[%d] = hw;"
          " _%s_draw_hh[%d] = hh; break;" % (
              j, idn, b["inst"], idn, b["inst"], idn, b["inst"]))
    p("    default: (void)tex; (void)hw; (void)hh; break;")
    p("    }")
    p("}")
    p("""/* ponytail: a transition's leftover time carries into its target
   scaled by the two speeds; Unity's own carry may differ by a frame */
/* A default state this cannot run is left to the default-clip player
   (`_build_animation_tables`), which runs or refuses it as before; only
   entering one later stops. */
static void engine_animator_tick(void) {
    int k, b;
    for (k = 0; k < %(nl)d; k = k + 1) {
        int s, guard = 0;
        float t, len;
        if (_AnimrL_pend[k] >= 0) {
            _animr_enter(k, _AnimrL_pend[k], _AnimrL_pend_t[k]);
            _AnimrL_pend[k] = -1;
        }
        s = _AnimrL_state[k];
        _AnimrL_time[k] = _AnimrL_time[k] + Time_deltaTime * _AnimrS_speed[s]
            * _Animr_speed[_AnimrL_anim[k]];
        while (_AnimrS_exit[s] >= 0 && guard < 8
               && _AnimrL_time[k] >= _AnimrS_exit_t[s] * _AnimrS_len[s]) {
            float carry = (_AnimrL_time[k] - _AnimrS_exit_t[s] * _AnimrS_len[s])
                / (_AnimrS_speed[s] != 0.f ? _AnimrS_speed[s] : 1.f);
            int d = _AnimrS_exit[s];
            _animr_enter(k, d, carry * _AnimrS_speed[d]);
            s = d;
            guard = guard + 1;
        }
        if (!_AnimrL_applies[k] || _AnimrS_why[s][0]) continue;
        len = _AnimrS_len[s];
        t = _AnimrL_time[k];
        if (_AnimrS_loop[s] && len > 0.f) t = t - len * floorf(t / len);
        else if (t > len) t = len;
        for (b = _AnimrS_fb[s]; b < _AnimrS_fb[s] + _AnimrS_fc[s]; b = b + 1)
            _animr_write_float(b, _animr_hermite(_AnimrFB_kb[b],
                                                 _AnimrFB_kc[b], t));
        for (b = _AnimrS_sb[s]; b < _AnimrS_sb[s] + _AnimrS_sc[s]; b = b + 1) {
            int kb = _AnimrSB_kb[b], kc = _AnimrSB_kc[b], j = kb;
            while (j + 1 < kb + kc && t >= _AnimrSK_t[j + 1]) j = j + 1;
            _animr_write_sprite(b, _AnimrSK_tex[j], _AnimrSK_hw[j],
                                _AnimrSK_hh[j]);
        }
    }
}
""".replace("%(nl)d", str(len(L))))


def _c_str(s):
    return cs2cpp.c_string(s)
