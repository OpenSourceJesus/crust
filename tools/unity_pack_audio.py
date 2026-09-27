# SPDX-License-Identifier: MIT
"""unity_pack: Audio: AudioSource tables and API rewrites.

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
from tools.unity_pack_anim import *  # noqa: E402,F401,F403

__all__ = [
    '_audiosource_field_init_index',
    '_build_audiosource_tables',
    '_rewrite_audiosource_api',
]


def _rewrite_audiosource_api(text, cl, add_locals=None):
    """Lower AudioSource playOnAwake/loop/volume/clip/Play/Stop / .gameObject."""
    as_recvs = set()
    for f in cl.get("fields") or []:
        if f.get("ty") == "AudioSource":
            as_recvs.add(f["name"])
    for name, _ty, _bits, kind in cl.get("members") or []:
        if str(kind) == "idx:AudioSource":
            as_recvs.add(name)
    for lm in re.finditer(r"\bAudioSource\s+(\w+)\b", text):
        as_recvs.add(lm.group(1))
    for name, ty in (add_locals or {}).items():
        if ty == "AudioSource":
            as_recvs.add(name)
    if not as_recvs:
        return text

    def repl_go(m):
        recv = m.group(1)
        if recv not in as_recvs:
            return m.group(0)
        return "_AudioSource_owner_go[%s]" % recv

    text = cs2cpp.code_sub(
        r"(?<![.\w])(\w+)\s*\.\s*gameObject\b",
        repl_go, text)

    bool_map = {
        "playOnAwake": "play_on_awake",
        "loop": "loop",
        "mute": "mute",
    }

    def repl_bool(m):
        recv, prop, rhs = m.group(1), m.group(2), m.group(3).strip()
        if recv not in as_recvs:
            return m.group(0)
        field = bool_map[prop]
        if rhs in ("false", "False", "0"):
            val = "0"
        elif rhs in ("true", "True", "1"):
            val = "1"
        else:
            val = "(%s) ? 1 : 0" % rhs
        return "_AudioSource_%s[%s] = %s;" % (field, recv, val)

    text = cs2cpp.code_sub(
        r"(?<![.\w])(\w+)\s*\.\s*(playOnAwake|loop|mute)\s*=\s*([^;]+);",
        repl_bool, text)

    def repl_float(m):
        recv, prop, rhs = m.group(1), m.group(2), m.group(3).strip()
        if recv not in as_recvs:
            return m.group(0)
        return "_AudioSource_%s[%s] = %s;" % (prop, recv, rhs)

    text = cs2cpp.code_sub(
        r"(?<![.\w])(\w+)\s*\.\s*(volume|pitch)\s*=\s*([^;]+);",
        repl_float, text)

    def repl_clip(m):
        recv, rhs = m.group(1), m.group(2).strip()
        if recv not in as_recvs:
            return m.group(0)
        if rhs == "null":
            rhs = "-1"
        return "_AudioSource_clip[%s] = %s;" % (recv, rhs)

    text = cs2cpp.code_sub(
        r"(?<![.\w])(\w+)\s*\.\s*clip\s*=\s*([^;]+);",
        repl_clip, text)

    def repl_call(m):
        recv, meth = m.group(1), m.group(2)
        if recv not in as_recvs:
            return m.group(0)
        return "AudioSource_%s(%s)" % (meth, recv)

    text = cs2cpp.code_sub(
        r"(?<![.\w])(\w+)\s*\.\s*(Play|Stop)\s*\(\s*\)",
        repl_call, text)

    # Bool/float reads: recv.volume / recv.loop
    def repl_read_float(m):
        recv, prop = m.group(1), m.group(2)
        if recv not in as_recvs:
            return m.group(0)
        return "_AudioSource_%s[%s]" % (prop, recv)

    text = cs2cpp.code_sub(
        r"(?<![.\w])(\w+)\s*\.\s*(volume|pitch)\b",
        repl_read_float, text)

    def repl_read_bool(m):
        recv, prop = m.group(1), m.group(2)
        if recv not in as_recvs:
            return m.group(0)
        field = bool_map[prop]
        return "_AudioSource_%s[%s]" % (field, recv)

    text = cs2cpp.code_sub(
        r"(?<![.\w])(\w+)\s*\.\s*(playOnAwake|loop|mute)\b",
        repl_read_bool, text)

    def repl_read_clip(m):
        recv = m.group(1)
        if recv not in as_recvs:
            return m.group(0)
        return "_AudioSource_clip[%s]" % recv

    text = cs2cpp.code_sub(
        r"(?<![.\w])(\w+)\s*\.\s*clip\b",
        repl_read_clip, text)
    return text


def _build_audiosource_tables(plan):
    """Authored AudioSource (!u!82) → packed pool; clip guids → opaque indices."""
    sources = []
    go_first = {}  # go_index → first AudioSource index (GetComponent)
    by_file_id = {}
    clip_guids = []
    clip_i = {}

    def _clip_idx(guid):
        g = (guid or "").lower()
        if not g:
            return -1
        if g not in clip_i:
            clip_i[g] = len(clip_guids)
            clip_guids.append(g)
        return clip_i[g]

    for cname, cl in sorted(plan["classes"].items()):
        for i, o in enumerate(cl.get("instances") or []):
            n = o.get("name") or "obj"
            gi = o.get("go_index")
            for a in o.get("audiosources") or []:
                fid = a.get("file_id")
                idx = len(sources)
                if gi is not None and int(gi) not in go_first:
                    go_first[int(gi)] = idx
                if fid is not None and str(fid) != "0":
                    by_file_id[str(fid)] = idx
                sources.append({
                    "name": n,
                    "go_index": gi,
                    "owner_class": cname,
                    "owner_inst": i,
                    "file_id": fid,
                    "play_on_awake": int(a.get("play_on_awake") or 0),
                    "volume": float(a.get("volume") or 1.0),
                    "pitch": float(a.get("pitch") or 1.0),
                    "loop": int(a.get("loop") or 0),
                    "mute": int(a.get("mute") or 0),
                    "clip": _clip_idx(a.get("clip_guid")),
                    "playing": 0,
                })
    return sources, go_first, by_file_id, clip_guids


def _audiosource_field_init_index(plan, o, fname):
    """Serialized AudioSource field → packed table index (-1 if missing)."""
    refs = o.get("object_refs") or {}
    fid = refs.get(fname)
    by_fid = plan.get("audiosource_by_file_id") or {}
    by_go = plan.get("go_audiosource") or {}
    if fid is not None and str(fid) != "0" and str(fid) in by_fid:
        return int(by_fid[str(fid)])
    n = o.get("name") or "obj"
    if n in by_go:
        return int(by_go[n])
    return -1
