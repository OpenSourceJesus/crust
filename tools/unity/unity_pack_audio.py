# SPDX-License-Identifier: MIT
"""unity_pack: Audio: AudioSource tables and API rewrites.

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
from tools.unity.unity_pack_anim import *  # noqa: E402,F401,F403

__all__ = [
    '_audiosource_field_init_index',
    '_build_audiosource_tables',
]


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
