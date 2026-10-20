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

from tools.unity.unity_pack_common import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_physics import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_sprites import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_ui import *  # noqa: E402,F401,F403

__all__ = [
    '_anim_sprite_guids',
    '_animator_sprite_guids',
    '_build_animator_tables',
    '_emit_animator_protos',
    '_parse_animator_machine',
    '_parse_float_curves',
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
