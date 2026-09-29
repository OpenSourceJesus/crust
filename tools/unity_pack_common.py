# SPDX-License-Identifier: MIT
"""unity_pack: Shared helpers for the unity_pack modules: errors, progress output, and small
leaf utilities that several modules use.

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

__all__ = [
    'PackError',
    'COOST_STRING_CORE',
    'coost_include_block',
    'coost_incdirs',
    'coost_string_core',
    'find_coost_root',
    'require_coost_root',
    '_B',
    '_EVENTTRIGGER_SCRIPT_GUID',
    '_UE',
    '_UNITY_BUILTIN_GUID',
    '_authored_camera_view_size',
    '_c_ident',
    '_class_name_from_cs',
    '_fit_preserve_aspect',
    '_is_unity_builtin_guid',
    '_layout_child_sizes',
    '_mb_enabled',
    '_mb_eventtrigger_callable',
    '_mb_index',
    '_mb_method_is_static',
    '_mb_onclick_callable',
    '_mb_onvaluechanged_callable',
    '_parse_pad_int',
    '_prefab_mod_float',
    '_progress',
    '_read',
    '_rect_pivot_center',
    '_sdf_coverage',
    '_split_call_args',
    '_ui_local_rect_wh',
    '_yaml_vec2',
    'player_display',
    'player_screen',
]


_split_call_args = cs2cpp.split_call_args


_c_ident = cs2cpp.c_ident


class PackError(Exception):
    def __init__(self, message):
        self.message = message
        Exception.__init__(self, message)



# --------------------------------------------------------------------------
# coost: the C++-subset library the engine's owned strings come from.
# An external checkout, found the way Box2D-Packed's is (`--box2d PATH`).
# --------------------------------------------------------------------------

#: coost sources a `fastring` needs, spliced into engine.cpp after its
#: headers. Relative to the checkout.
COOST_STRING_CORE = ("src/mem.cc", "src/fast.cc", "src/fastring.cc")

_COOST_URL = "https://github.com/crustos/coost"


def find_coost_root(coost_root=None):
    """coost checkout: *coost_root*, $COOST_ROOT, or a ``coost`` directory
    beside this repository. None when there is none."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [coost_root, os.environ.get("COOST_ROOT"),
                  os.path.join(os.path.dirname(here), "coost")]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "include", "co", "fastring.h")):
            return os.path.abspath(c)
    return None


def require_coost_root(coost_root=None):
    """The coost checkout, or PackError naming how to supply one.

    Upstream coost (idealvin/coost) has a `fastring.h` too, written in full
    C++ that cpprust refuses; the crust edition is told apart by
    `assign_cstr`, which only it has.
    """
    root = find_coost_root(coost_root)
    if not root:
        raise PackError(
            "C# string locals are stored as coost fastrings: pass --coost "
            "PATH, set COOST_ROOT, or clone %s beside this repository"
            % _COOST_URL)
    with open(os.path.join(root, "include", "co", "fastring.h")) as f:
        if "assign_cstr" not in f.read():
            raise PackError(
                "%s is not the crust edition of coost (no "
                "fastring::assign_cstr); clone %s" % (root, _COOST_URL))
    for rel in COOST_STRING_CORE:
        if not os.path.isfile(os.path.join(root, rel)):
            raise PackError("no %s in %s (coost checkout?)" % (rel, root))
    return root


def coost_incdirs(root):
    """Include path for splicing coost: its headers, and its root for the
    sources (`#include "src/fastring.cc"`)."""
    return [os.path.join(root, "include"), root]


def coost_string_core(root, extra=()):
    """coost's string core as one self-contained C++ text, for engine.cpp.

    Expanded here, on its own, rather than handing cpprust an include path
    for the whole engine: cpprust evaluates every simple `#if` in a file it
    splices headers into, and the engine's `#ifndef CRUST_NO_POSIX_MKDIR`
    -- decided by the compiler, host or crust -- would be decided for the
    host at lowering time, putting `<errno.h>` in front of crust's. It also
    makes engine.cpp stand alone, whichever checkout it was packed from.
    """
    import tools.cpprust as cpprust
    return cpprust._expand_headers(coost_include_block(extra), root,
                                   coost_incdirs(root))


def coost_include_block(extra=()):
    """The lines engine.cpp gets when it uses a fastring; `extra` are more
    coost sources the engine calls into (its hashes: `src/hash/md5.cc`)."""
    return ("/* coost string core (fastring), spliced in by cpprust */\n"
            "#include \"co/fastring.h\"\n"
            + "".join("#include \"%s\"\n" % rel
                      for rel in tuple(COOST_STRING_CORE) + tuple(extra)))

def _progress(msg):
    """Incremental status for long packs (large scenes / many PNGs)."""
    sys.stderr.write("unity_pack: %s\n" % msg)
    sys.stderr.flush()


def player_screen(root):
    """defaultScreenWidth / Height from ProjectSettings (Unity Player Settings).

    Missing keys → Unity standalone defaults 1024×768. Values ≤0 are clamped
    to 1 so hosts never create a zero-size window.
    """
    width, height, _fs, _native, _max = player_display(root)
    return width, height


def _authored_camera_view_size(objects):
    """GameCamera / CameraScript ``viewSize`` (world units), or None."""
    for o in objects or []:
        fields = o.get("fields") or {}
        if "viewSize_x" in fields and "viewSize_y" in fields:
            return float(fields["viewSize_x"]), float(fields["viewSize_y"])
    return None


def player_display(root):
    """Player Settings display tuple.

    Returns (width, height, fullscreen, native_resolution, maximized).

    fullscreenMode (Unity FullScreenMode):
      0 ExclusiveFullScreen, 1 FullScreenWindow → fullscreen 1
      2 MaximizedWindow → maximized 1
      3 Windowed / omitted → windowed (safe default for pack hosts / CI)
    defaultIsNativeResolution 1 → fullscreen hosts use the monitor video mode.
    """
    width, height = 1024, 768
    fullscreen_mode = 3  # Windowed when unset
    native = 1
    settings = os.path.join(root, "ProjectSettings", "ProjectSettings.asset")
    if os.path.isfile(settings):
        text = _read(settings)
        m = re.search(r"(?m)^\s*defaultScreenWidth:\s*(-?\d+)\s*$", text)
        if m:
            width = int(m.group(1))
        m = re.search(r"(?m)^\s*defaultScreenHeight:\s*(-?\d+)\s*$", text)
        if m:
            height = int(m.group(1))
        m = re.search(r"(?m)^\s*fullscreenMode:\s*(-?\d+)\s*$", text)
        if m:
            fullscreen_mode = int(m.group(1))
        m = re.search(
            r"(?m)^\s*defaultIsNativeResolution:\s*(-?\d+)\s*$", text)
        if m:
            native = int(m.group(1))
    if width < 1:
        width = 1
    if height < 1:
        height = 1
    fullscreen = 1 if fullscreen_mode in (0, 1) else 0
    maximized = 1 if fullscreen_mode == 2 else 0
    return width, height, fullscreen, (1 if native else 0), maximized


#: path -> text a pack reads instead of the file: C# rewritten at the
#: source level (tools/unity_pack_extensions.py). Set and cleared by pack().
SOURCE_OVERLAY = {}

#: API names a pack must emit because a static helper class uses them (its
#: body is inlined or copied into a caller the per-script scan read
#: without them). Set by pack().
SOURCE_API_HINTS = set()

#: The project's layer names ({index: name}), read by pack() from
#: ProjectSettings/TagManager.asset.
SOURCE_LAYER_NAMES = {}

#: The code-defined InputActions pack() found (tools/unity_pack_input.py).
SOURCE_INPUT_ACTIONS = []


def _read(path):
    if path in SOURCE_OVERLAY:
        return SOURCE_OVERLAY[path]
    ap = os.path.abspath(path)
    if ap in SOURCE_OVERLAY:
        return SOURCE_OVERLAY[ap]
    with open(path) as f:
        return f.read()


# UnityEngine.EventSystems.EventTrigger (UnityEngine.UI.dll).
_EVENTTRIGGER_SCRIPT_GUID = "d0b148fe25e99eb48b9724523833bab1"


# Unity "Resources/unity_builtin_extra" — UISprite, Background, Knob, …
_UNITY_BUILTIN_GUID = "0000000000000000f000000000000000"


def _is_unity_builtin_guid(guid):
    return (guid or "").lower() == _UNITY_BUILTIN_GUID


def _yaml_vec2(block, key, default=(0.0, 0.0)):
    m = re.search(
        r"(?m)^\s+%s:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}" % re.escape(key),
        block)
    if not m:
        return default
    return (float(m.group(1)), float(m.group(2)))


def _mb_enabled(block, default=1):
    """Authored Behaviour.m_Enabled (1 when YAML omits the field)."""
    en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
    return int(en.group(1)) if en else default


def _parse_pad_int(block, key, default=0):
    m = re.search(r"(?m)^\s+%s:\s*(-?\d+)" % re.escape(key), block)
    return int(m.group(1)) if m else default


def _rect_pivot_center(parent_w, parent_h, amin, amax, apos, size, pivot):
    """Canvas-local rect → (center_x, center_y, width, height) in parent pixels.

    Parent origin is bottom-left. Point anchors use sizeDelta as size; stretch
    anchors use (anchor span * parent) + sizeDelta.
    """
    ax0 = float(amin[0]) * parent_w
    ax1 = float(amax[0]) * parent_w
    ay0 = float(amin[1]) * parent_h
    ay1 = float(amax[1]) * parent_h
    if abs(ax1 - ax0) < 1e-6 and abs(ay1 - ay0) < 1e-6:
        w = float(size[0])
        h = float(size[1])
        pivot_x = ax0 + float(apos[0])
        pivot_y = ay0 + float(apos[1])
        cx = pivot_x + (0.5 - float(pivot[0])) * w
        cy = pivot_y + (0.5 - float(pivot[1])) * h
        return cx, cy, w, h
    w = (ax1 - ax0) + float(size[0])
    h = (ay1 - ay0) + float(size[1])
    cx = (ax0 + ax1) * 0.5 + float(apos[0])
    cy = (ay0 + ay1) * 0.5 + float(apos[1])
    return cx, cy, w, h


def _ui_local_rect_wh(o, by_xf, screen_w, screen_h, cache):
    """RectTransform.rect width/height before localScale (Unity layout space)."""
    key = "L:" + str(o.get("xf_id") or id(o))
    if key in cache:
        return cache[key]
    sw = float(screen_w)
    sh = float(screen_h)
    fid = o.get("father_id")
    parent = by_xf.get(str(fid)) if fid else None
    # Root Canvas fills the screen. Nested Canvas keeps RectTransform size
    # (Unity: Canvas does not override local rect).
    if o.get("canvas") and not (
            parent is not None and (
                parent.get("rect") is not None or parent.get("canvas"))):
        cache[key] = (sw, sh)
        return cache[key]
    if parent is not None and (
            parent.get("rect") is not None or parent.get("canvas")):
        pw, ph = _ui_local_rect_wh(
            parent, by_xf, screen_w, screen_h, cache)
    else:
        pw, ph = sw, sh
    rect = o.get("rect") or {}
    amin = rect.get("anchor_min") or (0.5, 0.5)
    amax = rect.get("anchor_max") or (0.5, 0.5)
    apos = rect.get("anchored_position") or (0.0, 0.0)
    size = rect.get("size_delta") or (100.0, 100.0)
    pivot = rect.get("pivot") or (0.5, 0.5)
    _lcx, _lcy, rw, rh = _rect_pivot_center(
        pw, ph, amin, amax, apos, size, pivot)
    cache[key] = (abs(float(rw)), abs(float(rh)))
    return cache[key]


def _layout_child_sizes(child, axis, control, force_expand):
    """(min, preferred, flexible) along *axis* — authored LayoutElement or sizeDelta."""
    le = child.get("layout_element") or {}
    if le.get("ignore"):
        return None
    # Disabled LayoutElement → same as no LayoutElement (sizeDelta / control).
    if not int(le.get("enabled", 1)):
        le = {}
    rect = child.get("rect") or {}
    sd = rect.get("size_delta") or (0.0, 0.0)
    cur = abs(float(sd[axis]))
    if not control:
        return cur, cur, 0.0
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
    if mx >= 0.0 and pref > mx:
        pref = mx
    if mx >= 0.0 and mn > mx:
        mn = mx
    if force_expand:
        flex = max(flex, 1.0)
    return mn, pref, flex


def _sdf_coverage(byte_v):
    """Approximate TMP SDF atlas byte → coverage (edge at ~0.5)."""
    t = byte_v / 255.0
    # smoothstep(0.45, 0.55, t)
    if t <= 0.45:
        return 0.0
    if t >= 0.55:
        return 1.0
    x = (t - 0.45) / 0.10
    return x * x * (3.0 - 2.0 * x)


def _fit_preserve_aspect(rw, rh, src_w, src_h):
    """Fit sprite into rect keeping aspect (Unity Image.preserveAspect).

    Returns (fit_w, fit_h) in the same units as *rw*/*rh*. Draw center is
    then shifted by ``_preserve_aspect_draw_center`` (Unity
    ``PreserveSpriteAspectRatio`` uses RectTransform.pivot, not always
    geometric center).
    """
    rw = abs(float(rw))
    rh = abs(float(rh))
    sw = float(src_w)
    sh = float(src_h)
    if rw < 1e-6 or rh < 1e-6 or sw < 1e-6 or sh < 1e-6:
        return rw, rh
    if rw / rh > sw / sh:
        # Rect wider than sprite → height-limited.
        return rh * (sw / sh), rh
    return rw, rw * (sh / sw)


def _prefab_mod_float(raw, path, default=None):
    """Last matching PrefabInstance modification float (root overrides win)."""
    matches = re.findall(
        r"propertyPath:\s*%s\s*\n\s*value:\s*([^\n]+)" % path, raw or "")
    if not matches:
        return default
    try:
        return float(matches[-1].strip())
    except ValueError:
        return default


def _mb_onclick_callable(analyses, cname, method, mode):
    """True if *method* on *cname* is a public instance API we can dispatch.

    PersistentListenerMode: 1=Void, 5=String, 6=Bool (SetActive path).
    Only Void / String MB calls are wired; Bool stays on GameObject.SetActive.
    """
    if not cname or not method or method == "SetActive":
        return False
    want_string = int(mode or 0) == 5
    want_void = int(mode or 0) in (0, 1)
    if not want_string and not want_void:
        return False
    for a in analyses or []:
        for c in a.get("classes") or []:
            if c.get("name") != cname:
                continue
            for m in c.get("methods") or []:
                if m.get("name") != method:
                    continue
                if not m.get("public") or m.get("static"):
                    continue
                args = (m.get("args") or "").strip()
                if want_string:
                    if re.match(
                            r"(?:System\.)?string\s+\w+\s*$", args, re.I):
                        return True
                elif want_void and not args:
                    return True
    return False


def _mb_eventtrigger_callable(analyses, cname, method, mode, object_arg_type=""):
    """True if EventTrigger can dispatch *method* (Void/Bool/Float/String/Object).

    Object mode: RectTransform → packed GO index int; AudioClip skipped
    (no clip table wired into MakeSoundEffect yet).
    """
    if not cname or not method or method == "SetActive":
        return False
    mode = int(mode or 0)
    oty = object_arg_type or ""
    if mode == 2:
        if "RectTransform" not in oty:
            return False
        # Accept any public instance method with one int-like / component arg.
        for a in analyses or []:
            for c in a.get("classes") or []:
                if c.get("name") != cname:
                    continue
                for m in c.get("methods") or []:
                    if m.get("name") != method:
                        continue
                    if not m.get("public") or m.get("static"):
                        continue
                    args = (m.get("args") or "").strip()
                    if re.match(
                            r"(?:UnityEngine\.)?(?:RectTransform|Transform|"
                            r"GameObject|int)\s+\w+\s*$",
                            args):
                        return True
        return False
    want_string = mode == 5
    want_bool = mode == 6
    want_float = mode == 4
    want_void = mode in (0, 1)
    if not (want_string or want_bool or want_float or want_void):
        return False
    for a in analyses or []:
        for c in a.get("classes") or []:
            if c.get("name") != cname:
                continue
            for m in c.get("methods") or []:
                if m.get("name") != method:
                    continue
                if not m.get("public") or m.get("static"):
                    continue
                args = (m.get("args") or "").strip()
                if want_string:
                    if re.match(
                            r"(?:System\.)?string\s+\w+\s*$", args, re.I):
                        return True
                elif want_bool:
                    if re.match(
                            r"(?:System\.)?bool\s+\w+\s*$", args, re.I):
                        return True
                elif want_float:
                    if re.match(
                            r"(?:System\.)?float\s+\w+\s*$", args, re.I):
                        return True
                elif want_void and not args:
                    return True
    return False


def _mb_onvaluechanged_callable(analyses, cname, method, mode):
    """True if *method* can be dispatched from Slider/Scrollbar.onValueChanged.

    UnityEvent<float>: mode 0/4 = EventDefined/Float (pass float);
    mode 1 = Void. Static property setters (``set_Volume``) are allowed.
    """
    if not cname or not method:
        return False
    mode = int(mode or 0)
    want_float = mode in (0, 4)
    want_void = mode == 1
    if not want_float and not want_void:
        return False
    for a in analyses or []:
        for c in a.get("classes") or []:
            if c.get("name") != cname:
                continue
            for m in c.get("methods") or []:
                if m.get("name") != method:
                    continue
                if not m.get("public"):
                    continue
                args = (m.get("args") or "").strip()
                if want_float:
                    if re.match(
                            r"(?:System\.)?float\s+\w+\s*$", args, re.I):
                        return True
                elif want_void and not args:
                    # Instance void only (OnValueChanged / SetDisplayValue).
                    if not m.get("static"):
                        return True
    return False


def _mb_method_is_static(analyses, cname, method):
    for a in analyses or []:
        for c in a.get("classes") or []:
            if c.get("name") != cname:
                continue
            for m in c.get("methods") or []:
                if m.get("name") == method and m.get("static"):
                    return True
    return False


def _class_name_from_cs(path):
    """The component a script file defines: the class named after the file.

    That is Unity's rule for a MonoBehaviour (`Bullet.cs` holds `Bullet`).
    The first class in the file used to be taken instead, so a helper
    declared above the component -- an attribute class for
    `[MaxInstances(N)]`, a small struct -- became the scene object's class.
    Without a class of the file's name, the first class, as before.
    """
    try:
        text = _read(path)
    except IOError:
        return None
    scan = cs2cpp._blank(text)
    stem = os.path.splitext(os.path.basename(path))[0]
    first = None
    for kind, name, _s, _b, _c in cs2cpp._find_types(scan):
        if kind in ("class", "struct"):
            if name == stem:
                return name
            if first is None:
                first = name
    return first


_B = cs2cpp.Binding


_UE = ("UnityEngine",)


def _mb_index(plan):
    """A script component's fileID -> (class, instance index)."""
    out = {}
    for cname, cl in (plan.get("classes") or {}).items():
        for i, o in enumerate(cl.get("instances") or []):
            for mb in o.get("mb_ids") or []:
                out[str(mb)] = (cname, i)
    return out
