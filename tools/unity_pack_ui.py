# SPDX-License-Identifier: MIT
"""unity_pack: uGUI: RectTransform, Canvas / CanvasScaler, Image, Button, Toggle, Slider,
Scrollbar, ScrollRect, EventTrigger, layout groups and TextMeshPro text.

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

__all__ = [
    '_BUTTON_SCRIPT_GUID',
    '_CANVAS_SCALER_GUID',
    '_IMAGE_SCRIPT_GUID',
    '_RECTTRANSFORM_SUPPORTED',
    '_RECT_VALUE_CALLS',
    '_SCROLLBAR_SCRIPT_GUID',
    '_SCROLLRECT_SCRIPT_GUID',
    '_SLIDER_SCRIPT_GUID',
    '_TMP_FONT_CACHE',
    '_TMP_UGUI_SCRIPT_GUID',
    '_TOGGLE_SCRIPT_GUID',
    '_UISPRITE_RADIUS',
    '_UISPRITE_SIZE',
    '_UI_COMPONENT_FIELD_TYPES',
    '_UI_GETCOMPONENT_TYPES',
    '_UNITY_API_RECT',
    '_annotate_ui_button_onclick_targets',
    '_annotate_ui_eventtrigger_targets',
    '_annotate_ui_persistent_mb_targets',
    '_annotate_ui_scrollbar_onvaluechanged_targets',
    '_annotate_ui_slider_onvaluechanged_targets',
    '_annotate_ui_toggle_onvaluechanged_targets',
    '_apply_rect_property_mods',
    '_apply_scrollbar_visuals',
    '_apply_slider_visuals',
    '_apply_ui_button_onclick_mods',
    '_apply_ui_image_sprite_mod',
    '_build_go_ui_component_maps',
    '_build_rect_transforms',
    '_build_ui_buttons',
    '_build_ui_eventtriggers',
    '_build_ui_scrollbars',
    '_build_ui_scrollrects',
    '_build_ui_sliders',
    '_build_ui_toggles',
    '_builtin_uisprite',
    '_bump_ui_hierarchy_draw_order',
    '_camera_script_rect',
    '_canvas_by_gameobject',
    '_canvas_scaler_layout_pixels',
    '_canvas_scaler_scale_factor',
    '_emit_rect_struct',
    '_find_canvas_scaler',
    '_go_is_canvas_root',
    '_is_canvas_scaler_mb',
    '_is_ui_button_mb',
    '_is_ui_eventtrigger_mb',
    '_is_ui_image_mb',
    '_is_ui_scrollbar_mb',
    '_is_ui_scrollrect_mb',
    '_is_ui_slider_mb',
    '_is_ui_tmp_mb',
    '_is_ui_toggle_mb',
    '_layout_group_calc_along_axis',
    '_link_scrollrects_scrollbars',
    '_load_tmp_font_asset',
    '_mb_ontoggle_callable',
    '_parse_canvas_scaler',
    '_parse_hv_layout_group',
    '_parse_ui_button',
    '_parse_ui_eventtrigger',
    '_parse_ui_scrollbar',
    '_parse_ui_scrollrect',
    '_parse_ui_slider',
    '_parse_ui_tmp',
    '_parse_ui_toggle',
    '_plan_has_ui_draws',
    '_prefab_mod_rect',
    '_rasterize_tmp_text',
    '_rect_receiver',
    '_rewrite_recttransform_apis',
    '_rewrite_toggle_is_on',
    '_slider_normalized',
    '_snapshot_ui_rects_onto_hierarchy',
    '_ui_layout_screen',
    '_ui_mb_call_resolve',
    '_ui_own_scale',
    '_ui_preserve_aspect_draw_size',
    '_ui_screen_rect',
    '_ui_xf_go_maps',
]


# MonoBehaviour.rectTransform members we lower (≡ GO index + live RT tables).
_RECTTRANSFORM_SUPPORTED = frozenset({
    "anchoredPosition", "sizeDelta", "localScale",
    "parent", "gameObject", "SetParent", "GetSiblingIndex", "Find",
})


# Authored uGUI / TMP component field types — scene-drawn, not packed MB arrays.
_UI_COMPONENT_FIELD_TYPES = frozenset((
    "Image", "RawImage", "Button", "Text", "Toggle", "Slider", "Scrollbar",
    "ScrollRect", "Dropdown", "InputField", "Mask", "RectMask2D",
    "Canvas", "CanvasGroup", "CanvasScaler", "GraphicRaycaster",
    "RectTransform", "Selectable",
    "TMP_Text", "TextMeshProUGUI", "TextMeshPro",
    "TMP_InputField", "TMP_Dropdown",
))


# GetComponent<T> for authored UI — opaque GO handles, not AddComponent invent.
_UI_GETCOMPONENT_TYPES = _UI_COMPONENT_FIELD_TYPES


def _find_canvas_scaler(objects):
    """Authored CanvasScaler on a Canvas GO, else any object, else None."""
    fallback = None
    for o in objects or []:
        cs = o.get("canvas_scaler")
        if not cs:
            continue
        if o.get("canvas"):
            return cs
        if fallback is None:
            fallback = cs
    return fallback


def _canvas_scaler_scale_factor(pixel_w, pixel_h, scaler):
    """Unity CanvasScaler.scaleFactor for the given pixel rect.

    Disabled / missing scaler → 1. Constant Physical Size is not modeled
    (returns 1). Scale With Screen Size matches Unity's log2 lerp /
    Expand / Shrink screen-match modes.
    """
    if not scaler or not int(scaler.get("enabled", 1)):
        return 1.0
    mode = int(scaler.get("ui_scale_mode") or 0)
    if mode == 0:  # Constant Pixel Size
        sf = float(scaler.get("scale_factor") or 1.0)
        return sf if sf > 1e-6 else 1.0
    if mode == 1:  # Scale With Screen Size
        rw = float(scaler.get("ref_x") or 800.0)
        rh = float(scaler.get("ref_y") or 600.0)
        if rw < 1e-6:
            rw = 1.0
        if rh < 1e-6:
            rh = 1.0
        pw, ph = float(pixel_w), float(pixel_h)
        if pw < 1e-6:
            pw = 1.0
        if ph < 1e-6:
            ph = 1.0
        smm = int(scaler.get("screen_match_mode") or 0)
        if smm == 1:  # Expand
            return min(pw / rw, ph / rh)
        if smm == 2:  # Shrink
            return max(pw / rw, ph / rh)
        # Match Width Or Height
        match = max(0.0, min(1.0, float(scaler.get("match") or 0.0)))
        log_w = math.log(pw / rw) / math.log(2.0)
        log_h = math.log(ph / rh) / math.log(2.0)
        return 2.0 ** (log_w * (1.0 - match) + log_h * match)
    return 1.0


def _canvas_scaler_layout_pixels(pixel_w, pixel_h, scaler):
    """Canvas root size in canvas units: pixelRect / scaleFactor."""
    sf = _canvas_scaler_scale_factor(pixel_w, pixel_h, scaler)
    if sf < 1e-6:
        sf = 1.0
    return (max(1, int(round(float(pixel_w) / sf))),
            max(1, int(round(float(pixel_h) / sf))))


def _ui_layout_screen(root, objects):
    """Screen size used for uGUI bake (Canvas Scaler / Camera.rect pixel size).

    When an authored CameraScript ``viewSize`` is present, start from the
    letterboxed camera pixel rect (e.g. 1920×960 for view 2:1 on 1920×1080).
    An enabled CanvasScaler then converts that pixel rect to canvas units
    (pixelRect / scaleFactor), matching Screen Space Camera + scaler.
    """
    sw, sh = player_screen(root)
    view = _authored_camera_view_size(objects)
    if view is not None:
        sw, sh = _camera_script_view_pixels(sw, sh, view[0], view[1])
    scaler = _find_canvas_scaler(objects)
    if scaler and int(scaler.get("enabled", 1)):
        return _canvas_scaler_layout_pixels(sw, sh, scaler)
    return sw, sh


def _camera_script_rect(screen_w, screen_h, view_w, view_h):
    """Normalized Camera.rect (x, y, w, h) for CameraScript.HandleViewSize."""
    sw = max(1, float(screen_w))
    sh = max(1, float(screen_h))
    vw = float(view_w)
    vh = float(view_h)
    if vw < 1e-6 or vh < 1e-6:
        return 0.0, 0.0, 1.0, 1.0
    cam_aspect = vw / vh
    screen_aspect = sw / sh
    rw = min(1.0, cam_aspect / screen_aspect)
    rh = min(1.0, screen_aspect / cam_aspect)
    return (0.5 - rw * 0.5), (0.5 - rh * 0.5), rw, rh


# Builtin uGUI Image / Button MonoBehaviour script guids (UnityEngine.UI.dll).
_IMAGE_SCRIPT_GUID = "fe87c0e1cc204ed48ad3b37840f39efc"


_BUTTON_SCRIPT_GUID = "4e29b1a8efbd4b44bb3f3716e73f07ff"


# UnityEngine.UI.Slider (handle/fill anchors driven by m_Value).
_SLIDER_SCRIPT_GUID = "67db9e8f0e2ae9c40bc1e2b64352a6b4"


# UnityEngine.UI.Scrollbar / ScrollRect / Toggle.
_SCROLLBAR_SCRIPT_GUID = "2a4db7a114972834c8e4117be1d82ba3"


_SCROLLRECT_SCRIPT_GUID = "1aa08ab6e0800fa44ae55d278d1423e3"


_TOGGLE_SCRIPT_GUID = "9085046f02f69544eb97fd06b6048fe2"


# TextMeshProUGUI (com.unity.ugui / Unity.TextMeshPro).
_TMP_UGUI_SCRIPT_GUID = "f4688fdb7df04437aeb418b961361dc5"


# uGUI CanvasScaler (UnityEngine.UI.dll).
_CANVAS_SCALER_GUID = "0cd44c1031e13a943bb63640046fad76"


def _is_ui_image_mb(block, guid):
    if (guid or "").lower() == _IMAGE_SCRIPT_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bImage\s*$", block))


def _is_ui_button_mb(block, guid):
    """True for builtin Button or a Button subclass (e.g. UIButton).

    Subclasses keep Selectable ColorBlock + Button.onClick in YAML; the
    EditorClassIdentifier is often ``…UIButton``, which does not match
    ``\\bButton`` (no word boundary inside the name).
    """
    if (guid or "").lower() == _BUTTON_SCRIPT_GUID:
        return True
    # UnityEngine.UI.Button (exact type name at end of identifier).
    if re.search(
            r"(?m)^\s+m_EditorClassIdentifier:.*(?:^|[.\s:])Button\s*$",
            block):
        return True
    # Button / Button-subclass serialization shape (not Toggle/Slider).
    if (re.search(r"(?m)^\s+m_Colors:\s*$", block)
            and re.search(r"(?m)^\s+m_OnClick:\s*$", block)):
        return True
    return False


def _is_ui_slider_mb(block, guid):
    """True for builtin Slider or a Slider subclass (e.g. ``_Slider``).

    Shape: ``m_HandleRect`` + ``m_MinValue`` (Scrollbar has HandleRect but
    not Min/MaxValue).
    """
    if (guid or "").lower() == _SLIDER_SCRIPT_GUID:
        return True
    if re.search(
            r"(?m)^\s+m_EditorClassIdentifier:.*(?:^|[.\s:])_?Slider\s*$",
            block):
        return True
    if (re.search(r"(?m)^\s+m_HandleRect:\s*", block)
            and re.search(r"(?m)^\s+m_MinValue:\s*", block)
            and re.search(r"(?m)^\s+m_MaxValue:\s*", block)):
        return True
    return False


def _parse_ui_slider(block, file_id=None):
    """Authored uGUI Slider → value range, direction, handle/fill, onValueChanged."""
    def _fid(key):
        m = re.search(
            r"(?m)^\s+%s:\s*\{fileID:\s*(-?\d+)" % re.escape(key), block)
        return int(m.group(1)) if m else 0

    def _f(key, default):
        m = re.search(
            r"(?m)^\s+%s:\s*([0-9.eE+-]+)" % re.escape(key), block)
        return float(m.group(1)) if m else float(default)

    def _i(key, default):
        m = re.search(r"(?m)^\s+%s:\s*(-?\d+)" % re.escape(key), block)
        return int(m.group(1)) if m else int(default)

    calls = []
    oc = re.search(r"(?m)^\s+m_OnValueChanged:\s*$", block)
    if oc:
        chunk = block[oc.end():]
        stop = re.search(r"(?m)^---\s", chunk)
        if stop:
            chunk = chunk[:stop.start()]
        # Stop at next sibling field of Slider / _Slider extras.
        stop2 = re.search(
            r"(?m)^\s+(?:displayValueText|selectable|slidingAreaRectTrs|"
            r"snapValues|indexOfCurrentSnapValue):\s*",
            chunk)
        if stop2:
            chunk = chunk[:stop2.start()]
        for cm in re.finditer(
                r"m_Target:\s*\{fileID:\s*(-?\d+)\}[\s\S]*?"
                r"m_MethodName:\s*(\w+)[\s\S]*?"
                r"m_Mode:\s*(\d+)",
                chunk):
            tid = int(cm.group(1))
            if tid == 0:
                continue
            calls.append({
                "target_go": str(tid),
                "method": cm.group(2),
                "mode": int(cm.group(3)),
            })
    interactable = _i("m_Interactable", 1)
    if not _mb_enabled(block):
        interactable = 0
    return {
        "enabled": _mb_enabled(block),
        "interactable": interactable,
        "handle_rect_id": _fid("m_HandleRect"),
        "fill_rect_id": _fid("m_FillRect"),
        # _Slider companion field; 0 → use handle parent / slider root.
        "slide_area_id": _fid("slidingAreaRectTrs"),
        "direction": _i("m_Direction", 0),
        "min": _f("m_MinValue", 0.0),
        "max": _f("m_MaxValue", 1.0),
        "value": _f("m_Value", 0.0),
        "whole_numbers": _i("m_WholeNumbers", 0),
        "on_value_changed": calls,
        "mb_file_id": file_id,
    }


def _is_ui_scrollbar_mb(block, guid):
    """True for builtin Scrollbar (HandleRect + Size, no Min/MaxValue)."""
    if (guid or "").lower() == _SCROLLBAR_SCRIPT_GUID:
        return True
    if re.search(
            r"(?m)^\s+m_EditorClassIdentifier:.*(?:^|[.\s:])Scrollbar\s*$",
            block):
        return True
    if (re.search(r"(?m)^\s+m_HandleRect:\s*", block)
            and re.search(r"(?m)^\s+m_Size:\s*", block)
            and not re.search(r"(?m)^\s+m_MinValue:\s*", block)):
        return True
    return False


def _parse_ui_scrollbar(block, file_id=None):
    """Authored uGUI Scrollbar → value/size/direction/handle/onValueChanged."""
    def _fid(key):
        m = re.search(
            r"(?m)^\s+%s:\s*\{fileID:\s*(-?\d+)" % re.escape(key), block)
        return int(m.group(1)) if m else 0

    def _f(key, default):
        m = re.search(
            r"(?m)^\s+%s:\s*([0-9.eE+-]+)" % re.escape(key), block)
        return float(m.group(1)) if m else float(default)

    def _i(key, default):
        m = re.search(r"(?m)^\s+%s:\s*(-?\d+)" % re.escape(key), block)
        return int(m.group(1)) if m else int(default)

    calls = []
    oc = re.search(r"(?m)^\s+m_OnValueChanged:\s*$", block)
    if oc:
        chunk = block[oc.end():]
        stop = re.search(r"(?m)^---\s", chunk)
        if stop:
            chunk = chunk[:stop.start()]
        for cm in re.finditer(
                r"m_Target:\s*\{fileID:\s*(-?\d+)\}[\s\S]*?"
                r"m_MethodName:\s*(\w+)[\s\S]*?"
                r"m_Mode:\s*(\d+)",
                chunk):
            tid = int(cm.group(1))
            if tid == 0:
                continue
            calls.append({
                "target_go": str(tid),
                "method": cm.group(2),
                "mode": int(cm.group(3)),
            })
    interactable = _i("m_Interactable", 1)
    enabled = _mb_enabled(block)
    if not enabled:
        interactable = 0
    return {
        "enabled": enabled,
        "interactable": interactable,
        "handle_rect_id": _fid("m_HandleRect"),
        "direction": _i("m_Direction", 0),
        "value": _f("m_Value", 0.0),
        "size": _f("m_Size", 0.2),
        "on_value_changed": calls,
        "mb_file_id": file_id,
    }


def _is_ui_scrollrect_mb(block, guid):
    if (guid or "").lower() == _SCROLLRECT_SCRIPT_GUID:
        return True
    if re.search(
            r"(?m)^\s+m_EditorClassIdentifier:.*(?:^|[.\s:])ScrollRect\s*$",
            block):
        return True
    if (re.search(r"(?m)^\s+m_Content:\s*", block)
            and re.search(r"(?m)^\s+m_Viewport:\s*", block)):
        return True
    return False


def _parse_ui_scrollrect(block, file_id=None):
    """Authored uGUI ScrollRect → content/viewport/scrollbar links."""
    def _fid(key):
        m = re.search(
            r"(?m)^\s+%s:\s*\{fileID:\s*(-?\d+)" % re.escape(key), block)
        return int(m.group(1)) if m else 0

    def _i(key, default):
        m = re.search(r"(?m)^\s+%s:\s*(-?\d+)" % re.escape(key), block)
        return int(m.group(1)) if m else int(default)

    return {
        "enabled": _mb_enabled(block),
        "content_id": _fid("m_Content"),
        "viewport_id": _fid("m_Viewport"),
        "horizontal": _i("m_Horizontal", 1),
        "vertical": _i("m_Vertical", 1),
        "hbar_mb_id": _fid("m_HorizontalScrollbar"),
        "vbar_mb_id": _fid("m_VerticalScrollbar"),
        "mb_file_id": file_id,
    }


def _is_ui_toggle_mb(block, guid):
    if (guid or "").lower() == _TOGGLE_SCRIPT_GUID:
        return True
    if re.search(
            r"(?m)^\s+m_EditorClassIdentifier:.*(?:^|[.\s:])Toggle\s*$",
            block):
        return True
    if (re.search(r"(?m)^\s+m_IsOn:\s*", block)
            and (re.search(r"(?m)^\s+onValueChanged:\s*$", block)
                 or re.search(r"(?m)^\s+m_OnValueChanged:\s*$", block))):
        return True
    return False


def _parse_ui_toggle(block, file_id=None):
    """Authored uGUI Toggle → isOn, graphic, onValueChanged (bool)."""
    def _fid(key):
        m = re.search(
            r"(?m)^\s+%s:\s*\{fileID:\s*(-?\d+)" % re.escape(key), block)
        return int(m.group(1)) if m else 0

    def _i(key, default):
        m = re.search(r"(?m)^\s+%s:\s*(-?\d+)" % re.escape(key), block)
        return int(m.group(1)) if m else int(default)

    calls = []
    oc = re.search(r"(?m)^\s+(?:m_)?OnValueChanged:\s*$", block)
    if not oc:
        oc = re.search(r"(?m)^\s+onValueChanged:\s*$", block)
    if oc:
        chunk = block[oc.end():]
        stop = re.search(r"(?m)^---\s", chunk)
        if stop:
            chunk = chunk[:stop.start()]
        stop2 = re.search(r"(?m)^\s+m_IsOn:\s*", chunk)
        if stop2:
            chunk = chunk[:stop2.start()]
        for cm in re.finditer(
                r"m_Target:\s*\{fileID:\s*(-?\d+)\}[\s\S]*?"
                r"m_MethodName:\s*(\w+)[\s\S]*?"
                r"m_Mode:\s*(\d+)[\s\S]*?"
                r"m_BoolArgument:\s*(\d+)",
                chunk):
            tid = int(cm.group(1))
            if tid == 0:
                continue
            calls.append({
                "target_go": str(tid),
                "method": cm.group(2),
                "mode": int(cm.group(3)),
                "bool_arg": int(cm.group(4)),
            })
    interactable = _i("m_Interactable", 1)
    if not _mb_enabled(block):
        interactable = 0
    graphic = _fid("graphic")
    if not graphic:
        graphic = _fid("m_Graphic")
    return {
        "enabled": _mb_enabled(block),
        "interactable": interactable,
        "is_on": _i("m_IsOn", 1),
        "graphic_id": graphic,
        "on_value_changed": calls,
        "mb_file_id": file_id,
    }


def _is_ui_eventtrigger_mb(block, guid):
    """True for UnityEngine.EventSystems.EventTrigger."""
    if (guid or "").lower() == _EVENTTRIGGER_SCRIPT_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bEventTrigger\s*$", block))


def _parse_ui_eventtrigger(block, file_id=None):
    """Authored EventTrigger → delegates (eventID + persistent calls).

    EventTriggerType: PointerEnter=0, Exit=1, Down=2, Up=3, Click=4,
    BeginDrag=13, EndDrag=14 (others ignored until needed).
    PersistentListenerMode: Void=1, Object=2, Float=4, String=5, Bool=6.
    """
    delegates = []
    dm = re.search(r"(?m)^\s+m_Delegates:\s*$", block)
    if not dm:
        return {
            "enabled": _mb_enabled(block),
            "delegates": [],
            "mb_file_id": file_id,
        }
    chunk = block[dm.end():]
    stop = re.search(r"(?m)^---\s", chunk)
    if stop:
        chunk = chunk[:stop.start()]
    parts = re.split(r"(?m)^  - eventID:\s*", chunk)
    for part in parts[1:]:
        em = re.match(r"(\d+)", part)
        if not em:
            continue
        event_id = int(em.group(1))
        calls = []
        for cm in re.finditer(
                r"m_Target:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*"
                r"([0-9a-fA-F]+))?[^}]*\}[\s\S]*?"
                r"m_TargetAssemblyTypeName:\s*([^\n]+)[\s\S]*?"
                r"m_MethodName:\s*(\w+)[\s\S]*?"
                r"m_Mode:\s*(\d+)[\s\S]*?"
                r"m_ObjectArgument:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*"
                r"([0-9a-fA-F]+))?[^}]*\}[\s\S]*?"
                r"m_ObjectArgumentAssemblyTypeName:\s*([^\n]+)[\s\S]*?"
                r"m_IntArgument:\s*(-?\d+)[\s\S]*?"
                r"m_FloatArgument:\s*([^\n]+)[\s\S]*?"
                r"m_StringArgument:\s*(.*)[\s\S]*?"
                r"m_BoolArgument:\s*(\d+)",
                part):
            tid = int(cm.group(1))
            if tid == 0 and not cm.group(2):
                continue
            try:
                farg = float(cm.group(10).strip())
            except ValueError:
                farg = 0.0
            calls.append({
                "target_go": str(tid),
                "target_guid": (cm.group(2) or "").lower(),
                "target_assembly": (cm.group(3) or "").strip(),
                "method": cm.group(4),
                "mode": int(cm.group(5)),
                "object_arg": str(int(cm.group(6))),
                "object_arg_guid": (cm.group(7) or "").lower(),
                "object_arg_type": (cm.group(8) or "").strip(),
                "int_arg": int(cm.group(9)),
                "float_arg": farg,
                "string_arg": (cm.group(11) or "").strip(),
                "bool_arg": int(cm.group(12)),
            })
        if calls:
            delegates.append({"event_id": event_id, "calls": calls})
    return {
        "enabled": _mb_enabled(block),
        "delegates": delegates,
        "mb_file_id": file_id,
    }


def _is_ui_tmp_mb(block, guid):
    if (guid or "").lower() == _TMP_UGUI_SCRIPT_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bTextMeshProUGUI\s*$", block))


def _is_canvas_scaler_mb(block, guid):
    g = (guid or "").lower()
    if g == _CANVAS_SCALER_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bCanvasScaler\s*$", block))


def _parse_hv_layout_group(block, vertical):
    """Authored Vertical/HorizontalLayoutGroup → bake dict."""
    sp = re.search(r"(?m)^\s+m_Spacing:\s*([0-9.eE+-]+)", block)
    return {
        "enabled": _mb_enabled(block),
        "vertical": bool(vertical),
        "pad_left": _parse_pad_int(block, "m_Left", 0),
        "pad_right": _parse_pad_int(block, "m_Right", 0),
        "pad_top": _parse_pad_int(block, "m_Top", 0),
        "pad_bottom": _parse_pad_int(block, "m_Bottom", 0),
        "spacing": float(sp.group(1)) if sp else 0.0,
        "child_alignment": _parse_pad_int(block, "m_ChildAlignment", 0),
        "child_force_expand_width": _parse_pad_int(
            block, "m_ChildForceExpandWidth", 1),
        "child_force_expand_height": _parse_pad_int(
            block, "m_ChildForceExpandHeight", 1),
        "child_control_width": _parse_pad_int(
            block, "m_ChildControlWidth", 1),
        "child_control_height": _parse_pad_int(
            block, "m_ChildControlHeight", 1),
        "child_scale_width": _parse_pad_int(
            block, "m_ChildScaleWidth", 0),
        "child_scale_height": _parse_pad_int(
            block, "m_ChildScaleHeight", 0),
        "reverse": _parse_pad_int(block, "m_ReverseArrangement", 0),
    }


def _parse_canvas_scaler(block):
    """Authored CanvasScaler → ui scale mode, reference resolution, match."""
    ref = _yaml_vec2(block, "m_ReferenceResolution", (800.0, 600.0))
    sf = re.search(r"(?m)^\s+m_ScaleFactor:\s*([0-9.eE+-]+)", block)
    match = re.search(
        r"(?m)^\s+m_MatchWidthOrHeight:\s*([0-9.eE+-]+)", block)
    return {
        "enabled": _mb_enabled(block),
        # 0 Constant Pixel Size, 1 Scale With Screen Size, 2 Constant Physical
        "ui_scale_mode": _parse_pad_int(block, "m_UiScaleMode", 0),
        "scale_factor": float(sf.group(1)) if sf else 1.0,
        "ref_x": float(ref[0]),
        "ref_y": float(ref[1]),
        # 0 Match Width Or Height, 1 Expand, 2 Shrink
        "screen_match_mode": _parse_pad_int(block, "m_ScreenMatchMode", 0),
        "match": float(match.group(1)) if match else 0.0,
    }


def _parse_ui_tmp(block, asset_guids):
    """Authored TextMeshProUGUI → text, font guid, color, size, alignment."""
    en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
    tm = re.search(r"(?m)^\s+m_text:\s*(.*)$", block)
    text = ""
    if tm:
        raw = tm.group(1).strip()
        if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
            text = raw[1:-1]
        elif raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            text = raw[1:-1]
        else:
            text = raw
    fg = re.search(
        r"m_fontAsset:\s*\{fileID:\s*-?\d+,\s*guid:\s*([0-9a-fA-F]+)",
        block)
    font_guid = fg.group(1).lower() if fg else None
    col = re.search(
        r"m_fontColor:\s*\{r:\s*([^,}]+),\s*g:\s*([^,}]+),"
        r"\s*b:\s*([^,}]+),\s*a:\s*([^}]+)\}", block)
    fs = re.search(r"(?m)^\s+m_fontSize:\s*([0-9.eE+-]+)", block)
    ha = re.search(r"(?m)^\s+m_HorizontalAlignment:\s*(\d+)", block)
    va = re.search(r"(?m)^\s+m_VerticalAlignment:\s*(\d+)", block)
    # 0 Overflow, 1 Ellipsis, 2 Masking, 3 Truncate, …
    ov = re.search(r"(?m)^\s+m_overflowMode:\s*(\d+)", block)
    has_font = bool(font_guid and font_guid in (asset_guids or {}))
    return {
        "text": text,
        "font_guid": font_guid,
        "has_font": has_font,
        "r": float(col.group(1)) if col else 1.0,
        "g": float(col.group(2)) if col else 1.0,
        "b": float(col.group(3)) if col else 1.0,
        "a": float(col.group(4)) if col else 1.0,
        "font_size": float(fs.group(1)) if fs else 14.0,
        "h_align": int(ha.group(1)) if ha else 1,
        "v_align": int(va.group(1)) if va else 256,
        "overflow_mode": int(ov.group(1)) if ov else 0,
        "enabled": int(en.group(1)) if en else 1,
    }


def _parse_ui_button(block, file_id=None):
    """Authored uGUI Button → interactable, ColorBlock, persistent onClick."""
    en = re.search(r"(?m)^\s+m_Interactable:\s*(\d+)", block)
    mb_en = _mb_enabled(block)

    def _col(key, default):
        m = re.search(
            r"%s:\s*\{r:\s*([^,}]+),\s*g:\s*([^,}]+),"
            r"\s*b:\s*([^,}]+),\s*a:\s*([^}]+)\}" % re.escape(key),
            block)
        if not m:
            return default
        return (float(m.group(1)), float(m.group(2)),
                float(m.group(3)), float(m.group(4)))

    mult = re.search(r"(?m)^\s+m_ColorMultiplier:\s*([0-9.eE+-]+)", block)
    colors = {
        "normal": _col("m_NormalColor", (1.0, 1.0, 1.0, 1.0)),
        "highlighted": _col("m_HighlightedColor",
                            (0.9607843, 0.9607843, 0.9607843, 1.0)),
        "pressed": _col("m_PressedColor",
                        (0.78431374, 0.78431374, 0.78431374, 1.0)),
        "selected": _col("m_SelectedColor",
                         (0.9607843, 0.9607843, 0.9607843, 1.0)),
        "disabled": _col("m_DisabledColor",
                         (0.78431374, 0.78431374, 0.78431374, 0.5019608)),
        "multiplier": float(mult.group(1)) if mult else 1.0,
    }
    calls = []
    oc = re.search(r"(?m)^\s+m_OnClick:\s*$", block)
    if oc:
        chunk = block[oc.end():]
        # End of this MonoBehaviour document (next ---) or next sibling field.
        stop = re.search(r"(?m)^---\s", chunk)
        if stop:
            chunk = chunk[:stop.start()]
        for cm in re.finditer(
                r"m_Target:\s*\{fileID:\s*(-?\d+)\}[\s\S]*?"
                r"m_MethodName:\s*(\w+)[\s\S]*?"
                r"m_Mode:\s*(\d+)[\s\S]*?"
                r"m_BoolArgument:\s*(\d+)",
                chunk):
            tid = int(cm.group(1))
            if tid == 0:
                continue
            span = cm.group(0)
            sm = re.search(r"m_StringArgument:\s*(.*)", span)
            string_arg = sm.group(1).strip() if sm else ""
            calls.append({
                "target_go": str(tid),
                "method": cm.group(2),
                "mode": int(cm.group(3)),
                "bool_arg": int(cm.group(4)),
                "string_arg": string_arg,
            })
    # Behaviour.enabled false → Selectable does not receive clicks.
    interactable = int(en.group(1)) if en else 1
    if not mb_en:
        interactable = 0
    return {
        "enabled": mb_en,
        "interactable": interactable,
        "colors": colors,
        "onclick": calls,
        # PrefabInstance m_OnClick mods target this MB fileID.
        "mb_file_id": file_id,
    }


def _ui_own_scale(o):
    """Abs RectTransform.localScale xy (Unity UI); zero → 1."""
    sc = o.get("local_scale") or o.get("scale") or (1.0, 1.0, 1.0)
    sx = abs(float(sc[0])) if len(sc) > 0 else 1.0
    sy = abs(float(sc[1])) if len(sc) > 1 else 1.0
    if sx < 1e-8:
        sx = 1.0
    if sy < 1e-8:
        sy = 1.0
    return sx, sy


def _ui_screen_rect(o, by_xf, screen_w, screen_h, cache):
    """Pixel rect (cx, cy, w, h) in screen space for a RectTransform object.

    Layout math uses parent ``rect`` (pre-localScale). Ancestor
    ``localScale`` accumulates into screen size — Unity Canvas space —
    so a VerticalLayoutGroup scaled to 0.59 shrinks children and TMP.
    """
    key = str(o.get("xf_id") or id(o))
    if key in cache:
        return cache[key]
    sw = float(screen_w)
    sh = float(screen_h)
    fid = o.get("father_id")
    parent = by_xf.get(str(fid)) if fid else None
    # Root Canvas pixel size is the screen (Overlay / Screen Space Camera).
    # Nested Canvas (e.g. Back Button sorting override) keeps RectTransform.
    if o.get("canvas") and not (
            parent is not None and (
                parent.get("rect") is not None or parent.get("canvas"))):
        cache[key] = (sw * 0.5, sh * 0.5, sw, sh)
        return cache[key]
    if parent is not None and (
            parent.get("rect") is not None or parent.get("canvas")):
        pcx, pcy, pw, ph = _ui_screen_rect(
            parent, by_xf, screen_w, screen_h, cache)
        plw, plh = _ui_local_rect_wh(
            parent, by_xf, screen_w, screen_h, cache)
        if plw < 1e-8:
            plw = 1e-8
        if plh < 1e-8:
            plh = 1e-8
        # parent.rect → screen (includes parent localScale + ancestors).
        fsx = abs(float(pw)) / plw
        fsy = abs(float(ph)) / plh
        plx = pcx - abs(float(pw)) * 0.5
        ply = pcy - abs(float(ph)) * 0.5
    else:
        # Root under Canvas / missing parent → full screen.
        plw, plh = sw, sh
        fsx, fsy = 1.0, 1.0
        plx, ply = 0.0, 0.0
    rect = o.get("rect") or {}
    amin = rect.get("anchor_min") or (0.5, 0.5)
    amax = rect.get("anchor_max") or (0.5, 0.5)
    apos = rect.get("anchored_position") or (0.0, 0.0)
    size = rect.get("size_delta") or (100.0, 100.0)
    pivot = rect.get("pivot") or (0.5, 0.5)
    # Child rect in parent.rect space (Unity LayoutGroup / anchors).
    lcx, lcy, rw, rh = _rect_pivot_center(
        plw, plh, amin, amax, apos, size, pivot)
    sx, sy = _ui_own_scale(o)
    rw0 = abs(float(rw))
    rh0 = abs(float(rh))
    # Unity localScale is about the pivot — the pivot stays fixed; the
    # geometric center moves toward it when scale ≠ 1 (center pivot: no move).
    px = float(pivot[0])
    py = float(pivot[1])
    lcx = float(lcx) + (0.5 - px) * rw0 * (sx - 1.0)
    lcy = float(lcy) + (0.5 - py) * rh0 * (sy - 1.0)
    rw = rw0 * sx
    rh = rh0 * sy
    cx = plx + float(lcx) * fsx
    cy = ply + float(lcy) * fsy
    cache[key] = (cx, cy, rw * fsx, rh * fsy)
    return cache[key]


def _go_is_canvas_root(o, by_xf):
    """True when Canvas fills the screen (no RectTransform/Canvas parent)."""
    if not o.get("canvas"):
        return False
    fid = o.get("father_id")
    parent = by_xf.get(str(fid)) if fid else None
    if parent is not None and (
            parent.get("rect") is not None or parent.get("canvas")):
        return False
    return True


def _snapshot_ui_rects_onto_hierarchy(objects, hierarchy):
    """Copy post-layout rect/scale/canvas onto hierarchy before scaffold drop.

    Layout-only Canvas / Rect parents are dropped from ``objects`` as
    ``ui_scaffold``, but live RT walks the GO parent chain. Snapshotting
    authored rect state onto ``scene_hierarchy`` (keyed by ``xf_id``) lets
    ``_build_rect_transforms`` seed after GO tables exist.
    """
    by_xf = {}
    for o in objects:
        xid = o.get("xf_id")
        if xid is not None and str(xid) not in ("", "0"):
            by_xf[str(xid)] = o
    for h in hierarchy or []:
        xid = h.get("xf_id")
        if xid is None or str(xid) in ("", "0"):
            continue
        o = by_xf.get(str(xid))
        if o is None:
            continue
        rect = o.get("rect")
        if rect is not None:
            h["rect"] = dict(rect)
        ls = o.get("local_scale") or o.get("scale")
        if ls is not None:
            h["local_scale"] = (
                float(ls[0]), float(ls[1]),
                float(ls[2]) if len(ls) > 2 else 1.0)
        if o.get("canvas"):
            h["has_canvas"] = True
            h["canvas_root"] = 1 if _go_is_canvas_root(o, by_xf) else 0
        elif rect is not None and "canvas_root" not in h:
            h["canvas_root"] = 0


def _build_rect_transforms(plan):
    """Per-GO RectTransform seed from hierarchy + packed instances (live)."""
    names = plan.get("go_names") or []
    n = len(names)
    if n < 1:
        return None
    has = [0] * n
    canvas = [0] * n
    canvas_root = [0] * n
    amin_x = [0.5] * n
    amin_y = [0.5] * n
    amax_x = [0.5] * n
    amax_y = [0.5] * n
    apos_x = [0.0] * n
    apos_y = [0.0] * n
    sd_x = [100.0] * n
    sd_y = [100.0] * n
    pivot_x = [0.5] * n
    pivot_y = [0.5] * n
    sx = [1.0] * n
    sy = [1.0] * n

    def _seed(gi, rect, local_scale, is_canvas, is_root):
        if gi is None or int(gi) < 0 or int(gi) >= n:
            return
        gi = int(gi)
        if rect is None and not is_canvas:
            return
        has[gi] = 1
        if is_canvas:
            canvas[gi] = 1
        if is_root:
            canvas_root[gi] = 1
        r = rect or {}
        amin = r.get("anchor_min") or (0.5, 0.5)
        amax = r.get("anchor_max") or (0.5, 0.5)
        apos = r.get("anchored_position") or (0.0, 0.0)
        size = r.get("size_delta") or (100.0, 100.0)
        pivot = r.get("pivot") or (0.5, 0.5)
        amin_x[gi] = float(amin[0])
        amin_y[gi] = float(amin[1])
        amax_x[gi] = float(amax[0])
        amax_y[gi] = float(amax[1])
        apos_x[gi] = float(apos[0])
        apos_y[gi] = float(apos[1])
        sd_x[gi] = float(size[0])
        sd_y[gi] = float(size[1])
        pivot_x[gi] = float(pivot[0])
        pivot_y[gi] = float(pivot[1])
        sc = local_scale or (1.0, 1.0, 1.0)
        sx[gi] = float(sc[0]) if len(sc) > 0 else 1.0
        sy[gi] = float(sc[1]) if len(sc) > 1 else 1.0

    # Hierarchy first — includes layout-only scaffolds snapshotted at drop.
    for h in plan.get("scene_hierarchy") or []:
        _seed(
            h.get("go_index"),
            h.get("rect"),
            h.get("local_scale"),
            bool(h.get("has_canvas")),
            int(h.get("canvas_root") or 0) != 0)
    # Packed instances may carry the same rect (post-layout); overlay.
    for cl in (plan.get("classes") or {}).values():
        for o in cl.get("instances") or []:
            is_root = False
            if o.get("canvas"):
                # Prefer hierarchy canvas_root when stamped; else recompute.
                gi = o.get("go_index")
                if gi is not None and 0 <= int(gi) < n and canvas_root[int(gi)]:
                    is_root = True
                else:
                    by_xf = {}
                    for oo in cl.get("instances") or []:
                        xid = oo.get("xf_id")
                        if xid is not None and str(xid) not in ("", "0"):
                            by_xf[str(xid)] = oo
                    for hh in plan.get("scene_hierarchy") or []:
                        xid = hh.get("xf_id")
                        if xid is not None and str(xid) not in ("", "0"):
                            by_xf.setdefault(str(xid), hh)
                    is_root = _go_is_canvas_root(o, by_xf)
            _seed(
                o.get("go_index"),
                o.get("rect"),
                o.get("local_scale") or o.get("scale"),
                bool(o.get("canvas")),
                is_root)
    if not any(has):
        return None
    return {
        "has": has,
        "canvas": canvas,
        "canvas_root": canvas_root,
        "amin_x": amin_x,
        "amin_y": amin_y,
        "amax_x": amax_x,
        "amax_y": amax_y,
        "apos_x": apos_x,
        "apos_y": apos_y,
        "sd_x": sd_x,
        "sd_y": sd_y,
        "pivot_x": pivot_x,
        "pivot_y": pivot_y,
        "sx": sx,
        "sy": sy,
    }


def _plan_has_ui_draws(plan):
    """True when any packed sprite is a baked uGUI Image/TMP draw."""
    for cl in (plan.get("classes") or {}).values():
        for o in cl.get("instances") or []:
            sp = o.get("sprite") or {}
            if sp.get("source") in ("ui", "ui_tmp"):
                return True
    return False


def _layout_group_calc_along_axis(parent, kids, axis):
    """Unity HorizontalOrVerticalLayoutGroup.CalcAlongAxis totals."""
    lg = parent.get("layout_group") or {}
    is_vert = bool(lg.get("vertical"))
    control = bool(lg.get(
        "child_control_width" if axis == 0 else "child_control_height", 1))
    force = bool(lg.get(
        "child_force_expand_width" if axis == 0
        else "child_force_expand_height", 1))
    use_scale = bool(lg.get(
        "child_scale_width" if axis == 0 else "child_scale_height", 0))
    spacing = float(lg.get("spacing") or 0.0)
    pad = ((float(lg.get("pad_left") or 0.0) + float(lg.get("pad_right") or 0.0))
           if axis == 0 else
           (float(lg.get("pad_top") or 0.0) + float(lg.get("pad_bottom") or 0.0)))
    along_other = bool(is_vert) ^ (axis == 1)
    total_min = pad
    total_pref = pad
    total_max = pad if not along_other else float("inf")
    total_flex = 0.0
    n = 0
    for ch in kids:
        sc = _layout_child_sizes(ch, axis, control, force)
        if sc is None:
            continue
        mn, pref, flex = sc
        le = ch.get("layout_element") or {}
        if not int(le.get("enabled", 1)):
            le = {}
        mx = float((le.get("max") or (-1.0, -1.0))[axis])
        if mx < 0.0:
            mx = float("inf")
        scale = 1.0
        if use_scale:
            ls = ch.get("local_scale") or (1.0, 1.0, 1.0)
            scale = abs(float(ls[axis]))
            if scale < 1e-8:
                scale = 1.0
        mn *= scale
        pref *= scale
        mx = mx * scale if mx < float("inf") else mx
        flex *= scale
        if along_other:
            total_min = max(mn + pad, total_min)
            if mx < float("inf"):
                total_max = min(mx + pad, total_max)
            total_pref = max(pref + pad, total_pref)
            total_flex = max(flex, total_flex)
        else:
            total_min += mn + spacing
            total_pref += pref + spacing
            if mx < float("inf"):
                total_max += mx + spacing
            else:
                total_max = float("inf")
            total_flex += flex
        n += 1
    if not along_other and n > 0:
        total_min -= spacing
        total_pref -= spacing
        if total_max < float("inf"):
            total_max -= spacing
    if total_max < float("inf"):
        if total_pref > total_max:
            total_pref = total_max
        if total_pref < total_min:
            total_pref = total_min
    return total_min, total_pref, total_max, total_flex


def _slider_normalized(value, vmin, vmax):
    """Unity Slider.normalizedValue."""
    lo = float(vmin)
    hi = float(vmax)
    if abs(hi - lo) < 1e-8:
        return 0.0
    t = (float(value) - lo) / (hi - lo)
    if t < 0.0:
        return 0.0
    if t > 1.0:
        return 1.0
    return t


def _apply_slider_visuals(objects):
    """Bake Unity ``Slider.UpdateVisuals`` into handle/fill RectTransforms.

    Scene YAML often leaves handle anchors at ``(0,0)-(0,0)`` because they
    are driven at runtime. Without this, pack places the knob at the
    Handle Slide Area's corner (often below/left of the track) instead of
    along ``normalizedValue``.
    """
    by_xf = {}
    for o in objects:
        xid = o.get("xf_id")
        if xid is not None and str(xid) not in ("", "0"):
            by_xf[str(xid)] = o
    for o in objects:
        sl = o.get("ui_slider")
        if not sl or not int(sl.get("enabled", 1)):
            continue
        direction = int(sl.get("direction") or 0)
        # 0 LTR, 1 RTL, 2 BTT, 3 TTB — UnityEngine.UI.Slider.Direction.
        axis = 0 if direction in (0, 1) else 1
        reverse = direction in (1, 3)
        nv = _slider_normalized(sl.get("value"), sl.get("min"), sl.get("max"))
        t = (1.0 - nv) if reverse else nv
        handle_id = int(sl.get("handle_rect_id") or 0)
        if handle_id:
            h = by_xf.get(str(handle_id))
            if h is not None and h.get("rect") is not None:
                rect = dict(h["rect"])
                amin = [0.0, 0.0]
                amax = [1.0, 1.0]
                amin[axis] = t
                amax[axis] = t
                rect["anchor_min"] = (float(amin[0]), float(amin[1]))
                rect["anchor_max"] = (float(amax[0]), float(amax[1]))
                h["rect"] = rect
        fill_id = int(sl.get("fill_rect_id") or 0)
        if fill_id:
            f = by_xf.get(str(fill_id))
            if f is not None and f.get("rect") is not None:
                rect = dict(f["rect"])
                amin = [0.0, 0.0]
                amax = [1.0, 1.0]
                if reverse:
                    amin[axis] = 1.0 - nv
                else:
                    amax[axis] = nv
                rect["anchor_min"] = (float(amin[0]), float(amin[1]))
                rect["anchor_max"] = (float(amax[0]), float(amax[1]))
                f["rect"] = rect


def _apply_scrollbar_visuals(objects):
    """Bake Unity ``Scrollbar.UpdateVisuals`` into handle RectTransform anchors.

    Handle spans ``size`` along the axis and sits at ``value * (1 - size)``.
    """
    by_xf = {}
    for o in objects:
        xid = o.get("xf_id")
        if xid is not None and str(xid) not in ("", "0"):
            by_xf[str(xid)] = o
    for o in objects:
        sb = o.get("ui_scrollbar")
        if not sb:
            continue
        direction = int(sb.get("direction") or 0)
        axis = 0 if direction in (0, 1) else 1
        reverse = direction in (1, 3)
        val = float(sb.get("value") or 0.0)
        if val < 0.0:
            val = 0.0
        if val > 1.0:
            val = 1.0
        size = float(sb.get("size") or 0.2)
        if size < 0.0:
            size = 0.0
        if size > 1.0:
            size = 1.0
        movement = val * (1.0 - size)
        handle_id = int(sb.get("handle_rect_id") or 0)
        if not handle_id:
            continue
        h = by_xf.get(str(handle_id))
        if h is None or h.get("rect") is None:
            continue
        rect = dict(h["rect"])
        amin = [0.0, 0.0]
        amax = [1.0, 1.0]
        if reverse:
            amin[axis] = 1.0 - movement - size
            amax[axis] = 1.0 - movement
        else:
            amin[axis] = movement
            amax[axis] = movement + size
        rect["anchor_min"] = (float(amin[0]), float(amin[1]))
        rect["anchor_max"] = (float(amax[0]), float(amax[1]))
        h["rect"] = rect


_TMP_FONT_CACHE = {}


def _load_tmp_font_asset(path):
    """Parse authored TMP Font Asset YAML → atlas + glyph metrics."""
    abspath = os.path.abspath(path)
    if abspath in _TMP_FONT_CACHE:
        return _TMP_FONT_CACHE[abspath]
    text = _read(path)
    point = re.search(r"(?m)^\s+m_PointSize:\s*([0-9.eE+-]+)", text)
    ascent = re.search(r"(?m)^\s+m_AscentLine:\s*([0-9.eE+-]+)", text)
    descent = re.search(r"(?m)^\s+m_DescentLine:\s*([0-9.eE+-]+)", text)
    line_h = re.search(r"(?m)^\s+m_LineHeight:\s*([0-9.eE+-]+)", text)
    aw = re.search(r"(?m)^\s+m_AtlasWidth:\s*(\d+)", text)
    ah = re.search(r"(?m)^\s+m_AtlasHeight:\s*(\d+)", text)
    tw = re.search(r"(?m)^\s+m_Width:\s*(\d+)", text)
    th = re.search(r"(?m)^\s+m_Height:\s*(\d+)", text)
    atlas_w = int(aw.group(1) if aw else (tw.group(1) if tw else 0))
    atlas_h = int(ah.group(1) if ah else (th.group(1) if th else 0))
    td = re.search(r"_typelessdata:\s*([0-9a-fA-F]+)", text)
    if not td or atlas_w < 1 or atlas_h < 1:
        _TMP_FONT_CACHE[abspath] = None
        return None
    hexdata = td.group(1)
    expect = atlas_w * atlas_h * 2  # Alpha8 → 2 hex chars per byte
    if len(hexdata) < expect:
        _TMP_FONT_CACHE[abspath] = None
        return None
    try:
        atlas = bytes.fromhex(hexdata[:expect])
    except ValueError:
        _TMP_FONT_CACHE[abspath] = None
        return None
    # Keep Texture2D row order (top-first). GlyphRect.y is from the top of
    # the atlas in TextCore / TMP font assets.
    chars = {}
    for m in re.finditer(
            r"m_Unicode:\s*(\d+)\s*\n\s+m_GlyphIndex:\s*(\d+)", text):
        chars[int(m.group(1))] = int(m.group(2))
    glyphs = {}
    for m in re.finditer(
            r"- m_Index:\s*(\d+)\s*\n\s+m_Metrics:\s*\n"
            r"\s+m_Width:\s*([^\n]+)\s*\n\s+m_Height:\s*([^\n]+)\s*\n"
            r"\s+m_HorizontalBearingX:\s*([^\n]+)\s*\n"
            r"\s+m_HorizontalBearingY:\s*([^\n]+)\s*\n"
            r"\s+m_HorizontalAdvance:\s*([^\n]+)\s*\n"
            r"\s+m_GlyphRect:\s*\n\s+m_X:\s*(\d+)\s*\n\s+m_Y:\s*(\d+)\s*\n"
            r"\s+m_Width:\s*(\d+)\s*\n\s+m_Height:\s*(\d+)",
            text):
        glyphs[int(m.group(1))] = {
            "w": float(m.group(2)), "h": float(m.group(3)),
            "bx": float(m.group(4)), "by": float(m.group(5)),
            "adv": float(m.group(6)),
            "rx": int(m.group(7)), "ry": int(m.group(8)),
            "rw": int(m.group(9)), "rh": int(m.group(10)),
        }
    font = {
        "path": abspath,
        "point_size": float(point.group(1)) if point else 72.0,
        "ascent": float(ascent.group(1)) if ascent else 0.0,
        "descent": float(descent.group(1)) if descent else 0.0,
        "line_height": float(line_h.group(1)) if line_h else 0.0,
        "atlas_w": atlas_w,
        "atlas_h": atlas_h,
        "atlas": atlas,
        "chars": chars,
        "glyphs": glyphs,
    }
    _TMP_FONT_CACHE[abspath] = font
    return font


def _rasterize_tmp_text(font, text, font_size, color, box_w, box_h,
                        h_align, v_align, overflow_mode=0):
    """Bake plain TMP string into an RGBA bitmap (y=0 bottom, OpenGL).

    Returns ``(bw, bh, rgba, shift_x, shift_y)`` where shift is the offset of
    the expanded bitmap center from the authored RectTransform center (pixels,
    +x right / +y up). Overflow mode 0 (TMP Overflow) expands the bake so
    glyphs that extend past the rect are not clipped — Unity still draws
    them; Truncate/Ellipsis/Masking keep the rect clip.
    """
    bw0 = max(1, int(round(float(box_w))))
    bh0 = max(1, int(round(float(box_h))))
    if not font or not text:
        return bw0, bh0, bytes(bytearray(bw0 * bh0 * 4)), 0.0, 0.0
    ps = float(font["point_size"]) or 1.0
    scale = float(font_size) / ps
    glyphs = []
    total_w = 0.0
    for ch in text:
        gi = font["chars"].get(ord(ch))
        if gi is None:
            continue
        g = font["glyphs"].get(gi)
        if not g:
            continue
        glyphs.append(g)
        total_w += float(g["adv"]) * scale
    ascent = float(font["ascent"]) * scale
    descent = float(font["descent"]) * scale  # typically negative
    visual_h = ascent - descent
    # Horizontal: 1 left, 2 center, 4 right (TMP bit flags).
    if h_align & 4:
        pen_x0 = float(bw0) - total_w
    elif h_align & 2:
        pen_x0 = (float(bw0) - total_w) * 0.5
    else:
        pen_x0 = 0.0
    # Vertical: 256 top, 512 middle, 1024 bottom — relative to authored rect.
    if v_align & 1024:
        baseline0 = -descent
    elif v_align & 512:
        baseline0 = (float(bh0) - visual_h) * 0.5 - descent
    else:
        baseline0 = float(bh0) - ascent
    # Glyph AABB in authored-rect pixel space (may extend past edges).
    min_x = 0.0
    min_y = 0.0
    max_x = float(bw0)
    max_y = float(bh0)
    pen = pen_x0
    for g in glyphs:
        gw = max(float(g["w"]) * scale, 0.0)
        gh = max(float(g["h"]) * scale, 0.0)
        gx0 = pen + float(g["bx"]) * scale
        gy1 = baseline0 + float(g["by"]) * scale
        gy0 = gy1 - gh
        if gx0 < min_x:
            min_x = gx0
        if gx0 + gw > max_x:
            max_x = gx0 + gw
        if gy0 < min_y:
            min_y = gy0
        if gy1 > max_y:
            max_y = gy1
        pen += float(g["adv"]) * scale
    # Overflow (0): expand. Other modes keep the rect (Unity clips / ellipsis).
    if int(overflow_mode or 0) == 0:
        pad_l = max(0.0, -min_x)
        pad_b = max(0.0, -min_y)
        pad_r = max(0.0, max_x - float(bw0))
        pad_t = max(0.0, max_y - float(bh0))
    else:
        pad_l = pad_b = pad_r = pad_t = 0.0
    bw = max(1, int(math.ceil(float(bw0) + pad_l + pad_r)))
    bh = max(1, int(math.ceil(float(bh0) + pad_b + pad_t)))
    # Expanded bitmap center vs authored rect center (screen +y up).
    shift_x = (pad_r - pad_l) * 0.5
    shift_y = (pad_t - pad_b) * 0.5
    pen_x = pen_x0 + pad_l
    baseline = baseline0 + pad_b
    out = bytearray(bw * bh * 4)
    aw = int(font["atlas_w"])
    ah = int(font["atlas_h"])
    atlas = font["atlas"]
    cr = float(color[0])
    cg = float(color[1])
    cb = float(color[2])
    ca = float(color[3])
    for g in glyphs:
        gw = max(float(g["w"]) * scale, 0.0)
        gh = max(float(g["h"]) * scale, 0.0)
        gx0 = pen_x + float(g["bx"]) * scale
        gy1 = baseline + float(g["by"]) * scale  # top
        gy0 = gy1 - gh  # bottom
        rx, ry, rw, rh = int(g["rx"]), int(g["ry"]), int(g["rw"]), int(g["rh"])
        # GlyphRect Y is from the top of the (top-first) atlas. Empirically
        # TMP SDF glyphs sample with v increasing toward the bottom of the
        # rect (matches LiberationSans SDF packing).
        for py in range(int(math.floor(gy0)), int(math.ceil(gy1))):
            if py < 0 or py >= bh:
                continue
            v = (py + 0.5 - gy0) / gh if gh > 1e-6 else 0.0
            if v < 0.0 or v > 1.0:
                continue
            sy = v * max(rh - 1, 0)
            for px in range(int(math.floor(gx0)), int(math.ceil(gx0 + gw))):
                if px < 0 or px >= bw:
                    continue
                u = (px + 0.5 - gx0) / gw if gw > 1e-6 else 0.0
                if u < 0.0 or u > 1.0:
                    continue
                sx = u * max(rw - 1, 0)
                ix = rx + int(round(sx))
                iy = ry + int(round(sy))
                if ix < 0 or iy < 0 or ix >= aw or iy >= ah:
                    continue
                cov = _sdf_coverage(atlas[iy * aw + ix])
                if cov <= 0.0:
                    continue
                o = (py * bw + px) * 4
                a = cov * ca
                out[o] = int(min(255, round(cr * 255.0 * cov)))
                out[o + 1] = int(min(255, round(cg * 255.0 * cov)))
                out[o + 2] = int(min(255, round(cb * 255.0 * cov)))
                out[o + 3] = int(min(255, round(a * 255.0)))
        pen_x += float(g["adv"]) * scale
    return bw, bh, bytes(out), shift_x, shift_y


# Unity builtin UISprite (UI/Skin/UISprite.psd): ~32×32 white rounded rect.
# Border matches the corner radius so Image.type=Sliced keeps fixed corners.
_UISPRITE_SIZE = 32


_UISPRITE_RADIUS = 6  # matches Unity UISprite corner / border scale


def _builtin_uisprite():
    """Generate Unity-like UISprite RGBA (y=0 bottom) + 9-slice border LBRT."""
    s = _UISPRITE_SIZE
    r = float(_UISPRITE_RADIUS)
    rgba = bytearray(s * s * 4)
    for y in range(s):
        # Atlas math in top-first space, then store bottom-first.
        yt = (s - 1 - y) + 0.5
        for x in range(s):
            xt = x + 0.5
            # Distance outside rounded rect (0 inside).
            cx = min(max(xt, r), s - r)
            cy = min(max(yt, r), s - r)
            dx = xt - cx
            dy = yt - cy
            dist = math.sqrt(dx * dx + dy * dy) - r
            # 1px AA fringe.
            if dist <= -0.5:
                a = 1.0
            elif dist >= 0.5:
                a = 0.0
            else:
                a = 0.5 - dist
            if a <= 0.0:
                continue
            o = (y * s + x) * 4
            v = int(min(255, round(a * 255.0)))
            rgba[o] = rgba[o + 1] = rgba[o + 2] = 255
            rgba[o + 3] = v
    border = (_UISPRITE_RADIUS,) * 4  # left, bottom, right, top
    return s, s, bytes(rgba), border


# Alias used by tests / older call sites.
_ui_preserve_aspect_draw_size = _fit_preserve_aspect


def _bump_ui_hierarchy_draw_order(objects, by_xf):
    """Raise child UI sorting_order above ancestor UI on the same layer."""
    # Parent before child: walk by increasing depth from roots.
    depth = {}

    def _depth(o):
        xid = str(o.get("xf_id") or id(o))
        if xid in depth:
            return depth[xid]
        fid = o.get("father_id")
        parent = by_xf.get(str(fid)) if fid else None
        d = 0 if parent is None else _depth(parent) + 1
        depth[xid] = d
        return d

    ordered = sorted(
        (o for o in objects
         if (o.get("sprite") or {}).get("source") in ("ui", "ui_tmp")),
        key=_depth)
    for o in ordered:
        sp = o.get("sprite") or {}
        # Nested Override Sorting sets an absolute order — leave it.
        own_c = o.get("canvas") or {}
        if int(own_c.get("override_sorting") or 0):
            continue
        fid = o.get("father_id")
        cur = by_xf.get(str(fid)) if fid else None
        guard = 0
        while cur is not None and guard < 64:
            guard += 1
            psp = cur.get("sprite") or {}
            if psp.get("source") in ("ui", "ui_tmp"):
                # Same canvas sorting layer (TagManager id on the sprite).
                if (int(psp.get("sorting_layer_id") or 0)
                        == int(sp.get("sorting_layer_id") or 0)):
                    po = int(psp.get("sorting_order") or 0)
                    so = int(sp.get("sorting_order") or 0)
                    if so <= po:
                        sp["sorting_order"] = po + 1
                break
            fid = cur.get("father_id")
            cur = by_xf.get(str(fid)) if fid else None


def _canvas_by_gameobject(by_id):
    """GameObject fileID → canvas dict from scene Canvas components.

    PrefabInstance ``m_AddedComponents`` Canvas blocks reference the stripped
    GO via ``m_GameObject``; the stripped GO YAML has no ``m_Component`` list,
    so the main join loop never sees them without this reverse map.
    """
    out = {}
    for rec in (by_id or {}).values():
        if rec.get("kind") != "Canvas" or not rec.get("canvas"):
            continue
        raw = rec.get("raw") or ""
        gm = re.search(
            r"(?m)^\s+m_GameObject:\s*\{fileID:\s*(\d+)\}", raw)
        if gm:
            out[str(gm.group(1))] = dict(rec["canvas"])
    return out


def _prefab_mod_rect(raw):
    """RectTransform fields from PrefabInstance m_Modifications."""
    def f(path, d):
        v = _prefab_mod_float(raw, path, None)
        return d if v is None else v

    return {
        "anchor_min": (f(r"m_AnchorMin\.x", 0.5), f(r"m_AnchorMin\.y", 0.5)),
        "anchor_max": (f(r"m_AnchorMax\.x", 0.5), f(r"m_AnchorMax\.y", 0.5)),
        "anchored_position": (
            f(r"m_AnchoredPosition\.x", 0.0),
            f(r"m_AnchoredPosition\.y", 0.0)),
        "size_delta": (
            f(r"m_SizeDelta\.x", 100.0), f(r"m_SizeDelta\.y", 100.0)),
        "pivot": (f(r"m_Pivot\.x", 0.5), f(r"m_Pivot\.y", 0.5)),
    }


def _apply_ui_image_sprite_mod(ui_image, inst_raw, asset_guids):
    """Apply authored PrefabInstance m_Sprite objectReference to *ui_image*."""
    if ui_image is None:
        ui_image = {
            "r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0,
            "enabled": 1, "has_sprite": False, "builtin": False,
            "sprite_file_id": 0, "sprite_guid": None,
            "image_type": 0, "pixels_per_unit_multiplier": 1.0,
        }
    else:
        ui_image = dict(ui_image)
    mb_id = str(ui_image.get("mb_file_id") or "")
    refs = _prefab_sprite_object_refs(inst_raw)
    chosen = None
    for target, spr_fid, sg in refs:
        if mb_id and str(target) == mb_id:
            chosen = (spr_fid, sg)
            break
    # Root Image often has empty m_Sprite; take the first override for this
    # instance when mb_file_id is unknown (still authored, not invented).
    if chosen is None and refs and not ui_image.get("has_sprite"):
        chosen = (refs[0][1], refs[0][2])
    if not chosen:
        return ui_image
    spr_fid, sg = chosen
    builtin = False
    has_sprite = False
    if sg and sg in (asset_guids or {}):
        has_sprite = True
    elif _is_unity_builtin_guid(sg):
        has_sprite = True
        builtin = True
    if not has_sprite:
        return ui_image
    ui_image["has_sprite"] = True
    ui_image["builtin"] = builtin
    ui_image["sprite_file_id"] = int(spr_fid)
    ui_image["sprite_guid"] = sg
    return ui_image


def _apply_ui_button_onclick_mods(ui_button, inst_raw, mb_file_id=None):
    """Merge PrefabInstance m_OnClick overrides onto a parsed ui_button.

    Prefab Button.onClick is often empty; scene instances add SetActive calls
    via propertyPath mods targeting the Button / UIButton MB fileID.
    """
    if not ui_button:
        return ui_button
    ui_button = dict(ui_button)
    mb_id = str(mb_file_id or ui_button.get("mb_file_id") or "")
    if not inst_raw or not mb_id:
        return ui_button
    # path → (value string, objectReference fileID)
    mods = {}
    for m in re.finditer(
            r"target:\s*\{fileID:\s*%s,[^}]*\}\s*\n"
            r"\s*propertyPath:\s*(m_OnClick[^\n]+)\s*\n"
            r"\s*value:\s*([^\n]*)\s*\n"
            r"\s*objectReference:\s*\{fileID:\s*(-?\d+)"
            % re.escape(mb_id),
            inst_raw):
        mods[m.group(1).strip()] = (m.group(2).strip(), int(m.group(3)))
    size_key = "m_OnClick.m_PersistentCalls.m_Calls.Array.size"
    if size_key not in mods:
        return ui_button
    try:
        n = int(float(mods[size_key][0]))
    except ValueError:
        return ui_button
    if n <= 0:
        ui_button["onclick"] = []
        return ui_button
    calls = []
    for i in range(n):
        prefix = ("m_OnClick.m_PersistentCalls.m_Calls.Array.data[%d]."
                  % i)
        method = (mods.get(prefix + "m_MethodName") or ("", 0))[0]
        if not method:
            continue
        mode_s = (mods.get(prefix + "m_Mode") or ("1", 0))[0]
        try:
            mode = int(float(mode_s))
        except ValueError:
            mode = 1
        bool_s = (mods.get(prefix + "m_Arguments.m_BoolArgument")
                  or ("0", 0))[0]
        try:
            bool_arg = int(float(bool_s))
        except ValueError:
            bool_arg = 0
        string_arg = (mods.get(prefix + "m_Arguments.m_StringArgument")
                      or ("", 0))[0]
        tgt_mod = mods.get(prefix + "m_Target")
        tid = int(tgt_mod[1]) if tgt_mod else 0
        if tid == 0:
            continue
        calls.append({
            "target_go": str(tid),
            "method": method,
            "mode": mode,
            "bool_arg": bool_arg,
            "string_arg": string_arg,
        })
    ui_button["onclick"] = calls
    return ui_button


def _annotate_ui_button_onclick_targets(objects, by_id, guid_to_script):
    """Tag each onClick call: GameObject vs project MonoBehaviour class.

    Persistent targets may be a GO (``SetActive``) or a script component
    fileID (including stripped PrefabInstance MBs). Prefer ``m_Script``
    guid — stripped blocks list the source-prefab guid first.
    """
    _annotate_ui_persistent_mb_targets(
        objects, by_id, guid_to_script, "ui_button", "onclick")


def _annotate_ui_slider_onvaluechanged_targets(objects, by_id, guid_to_script):
    """Tag each Slider onValueChanged call with target MB class."""
    _annotate_ui_persistent_mb_targets(
        objects, by_id, guid_to_script, "ui_slider", "on_value_changed")


def _annotate_ui_scrollbar_onvaluechanged_targets(
        objects, by_id, guid_to_script):
    _annotate_ui_persistent_mb_targets(
        objects, by_id, guid_to_script, "ui_scrollbar", "on_value_changed")


def _annotate_ui_toggle_onvaluechanged_targets(objects, by_id, guid_to_script):
    _annotate_ui_persistent_mb_targets(
        objects, by_id, guid_to_script, "ui_toggle", "on_value_changed")


def _annotate_ui_eventtrigger_targets(objects, by_id, guid_to_script):
    """Tag EventTrigger delegate calls with target MB class / Slider / GO."""
    if not objects or not by_id:
        return
    guid_to_script = guid_to_script or {}
    for o in objects:
        et = o.get("ui_eventtrigger")
        if not et:
            continue
        for d in et.get("delegates") or []:
            for c in d.get("calls") or []:
                tid = str(c.get("target_go") or "")
                asm = (c.get("target_assembly") or "").split(",")[0].strip()
                if asm.endswith(".Slider") or asm == "UnityEngine.UI.Slider":
                    if (c.get("method") or "") in ("set_value", "set_Value"):
                        c["target_kind"] = "slider"
                        continue
                if not tid or tid == "0":
                    tg = (c.get("target_guid") or "").lower()
                    sp = guid_to_script.get(tg) if tg else None
                    if sp:
                        cname = _class_name_from_cs(sp)
                        if cname:
                            c["target_kind"] = "mb"
                            c["target_class"] = cname
                    continue
                rec = by_id.get(tid)
                if not rec:
                    short = asm.split(".")[-1] if asm else ""
                    if short:
                        c["target_kind"] = "mb"
                        c["target_class"] = short
                    continue
                kind = rec.get("kind")
                if kind == "GameObject":
                    c["target_kind"] = "go"
                    continue
                if kind != "MonoBehaviour":
                    continue
                raw = rec.get("raw") or ""
                gm = re.search(
                    r"m_Script:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-fA-F]+)",
                    raw)
                g = (gm.group(1).lower() if gm
                     else (rec.get("guid") or "").lower())
                sp = guid_to_script.get(g) if g else None
                if not sp:
                    short = asm.split(".")[-1] if asm else ""
                    if short:
                        c["target_kind"] = "mb"
                        c["target_class"] = short
                    continue
                cname = _class_name_from_cs(sp)
                if cname:
                    c["target_kind"] = "mb"
                    c["target_class"] = cname
                oty = (c.get("object_arg_type") or "")
                if "RectTransform" in oty:
                    oid = str(c.get("object_arg") or "0")
                    trec = by_id.get(oid)
                    if trec and trec.get("kind") == "Transform":
                        rawt = trec.get("raw") or ""
                        gm2 = re.search(
                            r"(?m)^\s+m_GameObject:\s*\{fileID:\s*(-?\d+)\}",
                            rawt)
                        if gm2:
                            c["object_go"] = str(int(gm2.group(1)))


def _annotate_ui_persistent_mb_targets(
        objects, by_id, guid_to_script, obj_key, calls_key):
    """Tag persistent UnityEvent calls with target_kind / target_class."""
    if not objects or not by_id:
        return
    guid_to_script = guid_to_script or {}
    for o in objects:
        blob = o.get(obj_key)
        if not blob:
            continue
        for c in blob.get(calls_key) or []:
            tid = str(c.get("target_go") or "")
            if not tid or tid == "0":
                continue
            rec = by_id.get(tid)
            if not rec:
                continue
            kind = rec.get("kind")
            if kind == "GameObject":
                c["target_kind"] = "go"
                continue
            if kind != "MonoBehaviour":
                continue
            raw = rec.get("raw") or ""
            gm = re.search(
                r"m_Script:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-fA-F]+)",
                raw)
            g = (gm.group(1).lower() if gm
                 else (rec.get("guid") or "").lower())
            sp = guid_to_script.get(g) if g else None
            if not sp:
                continue
            cname = _class_name_from_cs(sp)
            if cname:
                c["target_kind"] = "mb"
                c["target_class"] = cname


def _mb_ontoggle_callable(analyses, cname, method, mode):
    """True if *method* can be dispatched from Toggle.onValueChanged.

    UnityEvent<bool>: mode 0 = EventDefined (pass isOn), mode 1 = Void,
    mode 6 = Bool (fixed m_BoolArgument).
    """
    if not cname or not method:
        return False
    mode = int(mode or 0)
    want_bool = mode in (0, 6)
    want_void = mode == 1
    if not want_bool and not want_void:
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
                if want_bool:
                    if re.match(
                            r"(?:System\.)?bool\s+\w+\s*$", args, re.I):
                        return True
                elif want_void and not args and not m.get("static"):
                    return True
    return False


def _apply_rect_property_mods(rect, scale, mods):
    """Mutate rect/scale from PrefabInstance propertyPath overrides."""
    rect = dict(rect or {})
    amin = list(rect.get("anchor_min") or (0.5, 0.5))
    amax = list(rect.get("anchor_max") or (0.5, 0.5))
    apos = list(rect.get("anchored_position") or (0.0, 0.0))
    size = list(rect.get("size_delta") or (0.0, 0.0))
    pivot = list(rect.get("pivot") or (0.5, 0.5))
    sc = list(scale or (1.0, 1.0, 1.0))
    while len(sc) < 3:
        sc.append(1.0)

    def _f(key, default=None):
        if key not in mods:
            return default
        try:
            return float(mods[key])
        except ValueError:
            return default

    for axis, idx in (("x", 0), ("y", 1)):
        v = _f("m_AnchorMin.%s" % axis)
        if v is not None:
            amin[idx] = v
        v = _f("m_AnchorMax.%s" % axis)
        if v is not None:
            amax[idx] = v
        v = _f("m_AnchoredPosition.%s" % axis)
        if v is not None:
            apos[idx] = v
        v = _f("m_SizeDelta.%s" % axis)
        if v is not None:
            size[idx] = v
        v = _f("m_Pivot.%s" % axis)
        if v is not None:
            pivot[idx] = v
        v = _f("m_LocalScale.%s" % axis)
        if v is not None:
            sc[idx] = v
    v = _f("m_LocalScale.z")
    if v is not None:
        sc[2] = v
    rect["anchor_min"] = (float(amin[0]), float(amin[1]))
    rect["anchor_max"] = (float(amax[0]), float(amax[1]))
    rect["anchored_position"] = (float(apos[0]), float(apos[1]))
    rect["size_delta"] = (float(size[0]), float(size[1]))
    rect["pivot"] = (float(pivot[0]), float(pivot[1]))
    return rect, (float(sc[0]), float(sc[1]), float(sc[2]))


def _build_go_ui_component_maps(plan, analyses=None):
    """Authored UI component presence: type → sorted GO indices."""
    maps = {t: set() for t in _UI_GETCOMPONENT_TYPES}

    def mark(gi, *tys):
        if gi is None or int(gi) < 0:
            return
        gi = int(gi)
        for t in tys:
            if t in maps:
                maps[t].add(gi)

    for cl in (plan.get("classes") or {}).values():
        for o in cl.get("instances") or []:
            gi = o.get("go_index")
            mark(gi, "RectTransform")
            if o.get("canvas"):
                mark(gi, "Canvas")
            if o.get("ui_image"):
                mark(gi, "Image", "RawImage", "Selectable")
            if o.get("ui_button"):
                mark(gi, "Button", "Selectable")
            if o.get("ui_tmp"):
                mark(gi, "TMP_Text", "TextMeshProUGUI", "TextMeshPro",
                     "Selectable")
    for h in plan.get("scene_hierarchy") or []:
        gi = h.get("go_index")
        mark(gi, "RectTransform")
        if h.get("has_canvas"):
            mark(gi, "Canvas")
        if h.get("has_image"):
            mark(gi, "Image", "RawImage", "Selectable")
        if h.get("has_button"):
            mark(gi, "Button", "Selectable")
        if h.get("has_tmp"):
            mark(gi, "TMP_Text", "TextMeshProUGUI", "TextMeshPro")
    # Project MB that subclasses a uGUI type (UIButton : Button): Unity's
    # GetComponent<Button>() finds it via inheritance.
    if analyses:
        bases = {}
        for a in analyses:
            for c in a.get("classes") or []:
                bases[c["name"]] = [
                    b for b in (c.get("bases") or [])
                    if b not in ("MonoBehaviour", "ScriptableObject",
                                 "object", "Object", "System")]
        memo = {}

        def ui_ancestor_types(cname):
            if cname in memo:
                return memo[cname]
            out = set()
            for b in bases.get(cname) or []:
                if b in maps:
                    out.add(b)
                out |= ui_ancestor_types(b)
            memo[cname] = out
            return out

        for cname, cl in (plan.get("classes") or {}).items():
            utys = ui_ancestor_types(cname)
            if not utys:
                continue
            for o in cl.get("instances") or []:
                mark(o.get("go_index"), *sorted(utys))
    return {t: sorted(s) for t, s in maps.items() if s}


def _build_ui_buttons(plan, analyses=None):
    """Authored uGUI Buttons: normalized hit, ColorBlock, onClick dispatch.

    Persistent ``SetActive`` targets resolve via GO fileID → go_index.
    MonoBehaviour targets (string/void modes) resolve via mb_ids or the
    annotated ``target_class`` from stripped PrefabInstance MBs.
    """
    go_by_id = {}
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            gid = str(o.get("go_id") or "")
            gi = o.get("go_index")
            if gid and gi is not None:
                go_by_id[gid] = int(gi)
    for h in plan.get("scene_hierarchy") or []:
        gid = str(h.get("go_id") or "")
        gi = h.get("go_index")
        if gid and gi is not None:
            go_by_id.setdefault(gid, int(gi))
    mb_index = _mb_index(plan)
    # class → first authored instance index (stripped MB fallback).
    class_inst0 = {}
    for cname, cl in (plan.get("classes") or {}).items():
        if int(cl.get("n") or 0) > 0:
            class_inst0[cname] = 0
    buttons = []
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            ub = o.get("ui_button")
            hit = o.get("ui_hit")
            if not ub or not hit:
                continue
            if not int(ub.get("interactable", 1)):
                continue
            self_go = o.get("go_index")
            if self_go is None:
                continue
            self_go = int(self_go)
            calls = []
            for c in ub.get("onclick") or []:
                method = c.get("method") or ""
                tid = str(c.get("target_go") or "")
                mode = int(c.get("mode") or 0)
                if method == "SetActive":
                    tgt = go_by_id.get(tid)
                    if tgt is None:
                        continue
                    calls.append({
                        "kind": "setactive",
                        "target_go": int(tgt),
                        "bool_arg": int(c.get("bool_arg") or 0),
                    })
                    continue
                # MonoBehaviour persistent call (String / Void).
                cname = c.get("target_class")
                inst = None
                hit_mb = mb_index.get(tid)
                if hit_mb:
                    cname, inst = hit_mb[0], int(hit_mb[1])
                elif cname and cname in class_inst0:
                    inst = int(class_inst0[cname])
                if cname is None or inst is None:
                    continue
                if not _mb_onclick_callable(analyses, cname, method, mode):
                    continue
                calls.append({
                    "kind": "mb",
                    "mb_class": cname,
                    "mb_inst": int(inst),
                    "method": method,
                    "mode": mode,
                    "string_arg": c.get("string_arg") or "",
                })
            # Tint / hit even when onClick has no resolvable calls (ColorBlock).
            cols = ub.get("colors") or {}
            mult = float(cols.get("multiplier") or 1.0)

            def _scale(key, default):
                c = cols.get(key) or default
                return tuple(float(c[i]) * mult for i in range(4))

            buttons.append({
                "go": self_go,
                "ncx": float(hit.get("ncx", 0.5)),
                "ncy": float(hit.get("ncy", 0.5)),
                "nhw": float(hit.get("nhw", 0.0)),
                "nhh": float(hit.get("nhh", 0.0)),
                "normal": _scale("normal", (1, 1, 1, 1)),
                "highlighted": _scale(
                    "highlighted", (0.96, 0.96, 0.96, 1)),
                "pressed": _scale("pressed", (0.78, 0.78, 0.78, 1)),
                "disabled": _scale(
                    "disabled", (0.78, 0.78, 0.78, 0.5)),
                "sorting_layer": int((o.get("sprite") or {}).get(
                    "sorting_layer") or 0),
                "sorting_order": int((o.get("sprite") or {}).get(
                    "sorting_order") or 0),
                "calls": calls,
            })
    buttons.sort(key=lambda b: (
        -int(b.get("sorting_layer") or 0),
        -int(b.get("sorting_order") or 0),
    ))
    return buttons


def _build_ui_sliders(plan, analyses=None):
    """Authored uGUI Sliders: drag hit GO, handle/fill, onValueChanged."""
    go_by_id = {}
    xf_to_go = {}
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            gid = str(o.get("go_id") or "")
            gi = o.get("go_index")
            if gid and gi is not None:
                go_by_id[gid] = int(gi)
            xid = o.get("xf_id")
            if xid is not None and gi is not None:
                xf_to_go[str(xid)] = int(gi)
    for h in plan.get("scene_hierarchy") or []:
        gid = str(h.get("go_id") or "")
        gi = h.get("go_index")
        if gid and gi is not None:
            go_by_id.setdefault(gid, int(gi))
        xid = h.get("xf_id")
        if xid is not None and gi is not None:
            xf_to_go.setdefault(str(xid), int(gi))
    # Handle RectTransform father → Handle Slide Area go_index.
    handle_parent = {}
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            xid = o.get("xf_id")
            fid = o.get("father_id")
            if xid is not None and fid is not None:
                handle_parent[str(xid)] = str(fid)
    for h in plan.get("scene_hierarchy") or []:
        xid = h.get("xf_id")
        fid = h.get("father_id")
        if xid is not None and fid is not None:
            handle_parent.setdefault(str(xid), str(fid))
    mb_index = _mb_index(plan)
    class_inst0 = {}
    for cname, cl in (plan.get("classes") or {}).items():
        if int(cl.get("n") or 0) > 0:
            class_inst0[cname] = 0
    sliders = []
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            sl = o.get("ui_slider")
            if not sl or not int(sl.get("enabled", 1)):
                continue
            if not int(sl.get("interactable", 1)):
                continue
            self_go = o.get("go_index")
            if self_go is None:
                continue
            self_go = int(self_go)
            handle_id = int(sl.get("handle_rect_id") or 0)
            fill_id = int(sl.get("fill_rect_id") or 0)
            slide_id = int(sl.get("slide_area_id") or 0)
            handle_go = xf_to_go.get(str(handle_id), -1) if handle_id else -1
            fill_go = xf_to_go.get(str(fill_id), -1) if fill_id else -1
            slide_go = -1
            if slide_id:
                slide_go = xf_to_go.get(str(slide_id), -1)
            if slide_go < 0 and handle_id:
                parent_xf = handle_parent.get(str(handle_id))
                if parent_xf:
                    slide_go = xf_to_go.get(parent_xf, -1)
            if slide_go < 0:
                slide_go = self_go
            calls = []
            for c in sl.get("on_value_changed") or []:
                method = c.get("method") or ""
                tid = str(c.get("target_go") or "")
                mode = int(c.get("mode") or 0)
                cname = c.get("target_class")
                inst = None
                hit_mb = mb_index.get(tid)
                if hit_mb:
                    cname, inst = hit_mb[0], int(hit_mb[1])
                elif cname and cname in class_inst0:
                    inst = int(class_inst0[cname])
                if cname is None or inst is None:
                    continue
                if not _mb_onvaluechanged_callable(
                        analyses, cname, method, mode):
                    continue
                is_static = _mb_method_is_static(analyses, cname, method)
                calls.append({
                    "kind": "mb",
                    "mb_class": cname,
                    "mb_inst": int(inst),
                    "method": method,
                    "mode": mode,
                    "static": bool(is_static),
                })
            direction = int(sl.get("direction") or 0)
            sliders.append({
                "go": self_go,
                "slide_go": int(slide_go),
                "handle_go": int(handle_go),
                "fill_go": int(fill_go),
                "direction": direction,
                "min": float(sl.get("min") or 0.0),
                "max": float(sl.get("max") or 1.0),
                "value": float(sl.get("value") or 0.0),
                "whole_numbers": int(sl.get("whole_numbers") or 0),
                "calls": calls,
            })
    return sliders


def _ui_xf_go_maps(plan):
    """go_id→go_index, xf_id→go_index, handle_parent xf→father xf."""
    go_by_id = {}
    xf_to_go = {}
    handle_parent = {}
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            gid = str(o.get("go_id") or "")
            gi = o.get("go_index")
            if gid and gi is not None:
                go_by_id[gid] = int(gi)
            xid = o.get("xf_id")
            if xid is not None and gi is not None:
                xf_to_go[str(xid)] = int(gi)
            fid = o.get("father_id")
            if xid is not None and fid is not None:
                handle_parent[str(xid)] = str(fid)
    for h in plan.get("scene_hierarchy") or []:
        gid = str(h.get("go_id") or "")
        gi = h.get("go_index")
        if gid and gi is not None:
            go_by_id.setdefault(gid, int(gi))
        xid = h.get("xf_id")
        if xid is not None and gi is not None:
            xf_to_go.setdefault(str(xid), int(gi))
        fid = h.get("father_id")
        if xid is not None and fid is not None:
            handle_parent.setdefault(str(xid), str(fid))
    return go_by_id, xf_to_go, handle_parent


def _ui_mb_call_resolve(plan, analyses, calls, callable_fn):
    """Resolve persistent UnityEvent calls → mb dispatch entries."""
    mb_index = _mb_index(plan)
    class_inst0 = {}
    for cname, cl in (plan.get("classes") or {}).items():
        if int(cl.get("n") or 0) > 0:
            class_inst0[cname] = 0
    out = []
    for c in calls or []:
        method = c.get("method") or ""
        tid = str(c.get("target_go") or "")
        mode = int(c.get("mode") or 0)
        cname = c.get("target_class")
        inst = None
        hit_mb = mb_index.get(tid)
        if hit_mb:
            cname, inst = hit_mb[0], int(hit_mb[1])
        elif cname and cname in class_inst0:
            inst = int(class_inst0[cname])
        if cname is None or inst is None:
            continue
        if not callable_fn(analyses, cname, method, mode):
            continue
        out.append({
            "kind": "mb",
            "mb_class": cname,
            "mb_inst": int(inst),
            "method": method,
            "mode": mode,
            "static": bool(_mb_method_is_static(analyses, cname, method)),
            "bool_arg": int(c.get("bool_arg") or 0),
        })
    return out


def _build_ui_scrollbars(plan, analyses=None):
    """Authored uGUI Scrollbars: handle drag + onValueChanged."""
    _go_by_id, xf_to_go, handle_parent = _ui_xf_go_maps(plan)
    bars = []
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            sb = o.get("ui_scrollbar")
            if not sb:
                continue
            self_go = o.get("go_index")
            if self_go is None:
                continue
            self_go = int(self_go)
            handle_id = int(sb.get("handle_rect_id") or 0)
            handle_go = xf_to_go.get(str(handle_id), -1) if handle_id else -1
            slide_go = -1
            if handle_id:
                parent_xf = handle_parent.get(str(handle_id))
                if parent_xf:
                    slide_go = xf_to_go.get(parent_xf, -1)
            if slide_go < 0:
                slide_go = self_go
            calls = _ui_mb_call_resolve(
                plan, analyses, sb.get("on_value_changed"),
                _mb_onvaluechanged_callable)
            bars.append({
                "go": self_go,
                "slide_go": int(slide_go),
                "handle_go": int(handle_go),
                "direction": int(sb.get("direction") or 0),
                "value": float(sb.get("value") or 0.0),
                "size": float(sb.get("size") or 0.2),
                "interactable": int(sb.get("interactable", 1)),
                "mb_file_id": str(sb.get("mb_file_id") or ""),
                "calls": calls,
            })
    return bars


def _build_ui_scrollrects(plan, analyses=None):
    """Authored uGUI ScrollRects: viewport drag + linked scrollbars."""
    _go_by_id, xf_to_go, _hp = _ui_xf_go_maps(plan)
    # Scrollbar MB fileID → index in ui_scrollbars.
    sb_by_mb = {}
    for i, sb in enumerate(plan.get("ui_scrollbars") or []):
        mid = str(sb.get("mb_file_id") or "")
        if mid and mid != "None":
            sb_by_mb[mid] = i
    rects = []
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            sr = o.get("ui_scrollrect")
            if not sr or not int(sr.get("enabled", 1)):
                continue
            self_go = o.get("go_index")
            if self_go is None:
                continue
            self_go = int(self_go)
            content_id = int(sr.get("content_id") or 0)
            viewport_id = int(sr.get("viewport_id") or 0)
            content_go = (
                xf_to_go.get(str(content_id), -1) if content_id else -1)
            viewport_go = (
                xf_to_go.get(str(viewport_id), -1) if viewport_id else -1)
            if viewport_go < 0:
                viewport_go = self_go
            hbar = sb_by_mb.get(str(sr.get("hbar_mb_id") or ""), -1)
            vbar = sb_by_mb.get(str(sr.get("vbar_mb_id") or ""), -1)
            rects.append({
                "go": self_go,
                "content_go": int(content_go),
                "viewport_go": int(viewport_go),
                "horizontal": int(sr.get("horizontal", 1)),
                "vertical": int(sr.get("vertical", 1)),
                "hbar": int(hbar) if hbar is not None else -1,
                "vbar": int(vbar) if vbar is not None else -1,
            })
    return rects


def _build_ui_toggles(plan, analyses=None):
    """Authored uGUI Toggles: click to flip isOn + onValueChanged(bool)."""
    _go_by_id, xf_to_go, _hp = _ui_xf_go_maps(plan)
    # Image MB fileID → go_index (Toggle.graphic).
    img_mb_to_go = {}
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            ui = o.get("ui_image")
            gi = o.get("go_index")
            if not ui or gi is None:
                continue
            mid = str(ui.get("mb_file_id") or "")
            if mid and mid not in ("", "None", "0"):
                img_mb_to_go[mid] = int(gi)
    toggles = []
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            tg = o.get("ui_toggle")
            if not tg or not int(tg.get("enabled", 1)):
                continue
            if not int(tg.get("interactable", 1)):
                continue
            self_go = o.get("go_index")
            if self_go is None:
                continue
            self_go = int(self_go)
            gid = int(tg.get("graphic_id") or 0)
            graphic_go = -1
            if gid:
                graphic_go = img_mb_to_go.get(str(gid), -1)
                if graphic_go < 0:
                    graphic_go = xf_to_go.get(str(gid), -1)
            calls = _ui_mb_call_resolve(
                plan, analyses, tg.get("on_value_changed"),
                _mb_ontoggle_callable)
            toggles.append({
                "go": self_go,
                "is_on": int(tg.get("is_on") or 0),
                "graphic_go": int(graphic_go),
                "calls": calls,
            })
    return toggles


def _build_ui_eventtriggers(plan, analyses=None):
    """Authored EventTrigger → hit GO + per-event persistent calls.

    Supported eventIDs: PointerEnter/Exit/Down/Up/Click, BeginDrag, EndDrag.
    Object(RectTransform) → GO index int; AudioClip object args skipped.
    """
    go_by_id, _xf, _hp = _ui_xf_go_maps(plan)
    mb_index = _mb_index(plan)
    class_inst0 = {}
    for cname, cl in (plan.get("classes") or {}).items():
        if int(cl.get("n") or 0) > 0:
            class_inst0[cname] = 0
    # Slider go → ui_sliders index for set_value.
    sl_by_go = {}
    for i, sl in enumerate(plan.get("ui_sliders") or []):
        sl_by_go[int(sl["go"])] = i
    # Also map Slider MB fileID → slider index via instances.
    sl_by_mb = {}
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            us = o.get("ui_slider")
            if not us:
                continue
            gi = o.get("go_index")
            if gi is None:
                continue
            si = sl_by_go.get(int(gi))
            if si is None:
                continue
            mid = str(us.get("mb_file_id") or "")
            if mid:
                sl_by_mb[mid] = si
            for mid2 in o.get("mb_ids") or []:
                sl_by_mb[str(mid2)] = si
    supported = frozenset({0, 1, 2, 3, 4, 13, 14})
    triggers = []
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            et = o.get("ui_eventtrigger")
            hit = o.get("ui_hit")
            if not et or not hit:
                continue
            if not int(et.get("enabled", 1)):
                continue
            self_go = o.get("go_index")
            if self_go is None:
                continue
            self_go = int(self_go)
            events = []
            for d in et.get("delegates") or []:
                # eventID 0 (PointerEnter) is valid — do not use `or -1`.
                raw_eid = d.get("event_id")
                if raw_eid is None:
                    continue
                eid = int(raw_eid)
                if eid not in supported:
                    continue
                calls = []
                for c in d.get("calls") or []:
                    method = c.get("method") or ""
                    mode = int(c.get("mode") or 0)
                    tid = str(c.get("target_go") or "")
                    if c.get("target_kind") == "slider" or (
                            method in ("set_value", "set_Value")
                            and mode == 4):
                        si = sl_by_mb.get(tid)
                        if si is None and tid in go_by_id:
                            si = sl_by_go.get(go_by_id[tid])
                        if si is None:
                            continue
                        calls.append({
                            "kind": "slider_set",
                            "slider": int(si),
                            "float_arg": float(c.get("float_arg") or 0.0),
                        })
                        continue
                    if method == "SetActive":
                        tgt = go_by_id.get(tid)
                        if tgt is None:
                            continue
                        calls.append({
                            "kind": "setactive",
                            "target_go": int(tgt),
                            "bool_arg": int(c.get("bool_arg") or 0),
                        })
                        continue
                    cname = c.get("target_class")
                    inst = None
                    hit_mb = mb_index.get(tid)
                    if hit_mb:
                        cname, inst = hit_mb[0], int(hit_mb[1])
                    elif cname and cname in class_inst0:
                        inst = int(class_inst0[cname])
                    if cname is None or inst is None:
                        continue
                    if not _mb_eventtrigger_callable(
                            analyses, cname, method, mode,
                            c.get("object_arg_type") or ""):
                        continue
                    entry = {
                        "kind": "mb",
                        "mb_class": cname,
                        "mb_inst": int(inst),
                        "method": method,
                        "mode": mode,
                        "bool_arg": int(c.get("bool_arg") or 0),
                        "float_arg": float(c.get("float_arg") or 0.0),
                        "string_arg": c.get("string_arg") or "",
                        "object_go": -1,
                    }
                    if mode == 2:
                        og = c.get("object_go")
                        if og and str(og) in go_by_id:
                            entry["object_go"] = int(go_by_id[str(og)])
                        else:
                            continue
                    calls.append(entry)
                if calls:
                    events.append({"event_id": eid, "calls": calls})
            if not events:
                continue
            triggers.append({
                "go": self_go,
                "ncx": float(hit.get("ncx", 0.5)),
                "ncy": float(hit.get("ncy", 0.5)),
                "nhw": float(hit.get("nhw", 0.0)),
                "nhh": float(hit.get("nhh", 0.0)),
                "sorting_layer": int((o.get("sprite") or {}).get(
                    "sorting_layer") or 0),
                "sorting_order": int((o.get("sprite") or {}).get(
                    "sorting_order") or 0),
                "events": events,
            })
    triggers.sort(key=lambda t: (
        -int(t.get("sorting_layer") or 0),
        -int(t.get("sorting_order") or 0),
    ))
    return triggers


def _link_scrollrects_scrollbars(plan):
    """Annotate scrollbars with owning ScrollRect index + axis."""
    bars = plan.get("ui_scrollbars") or []
    for b in bars:
        b["scrollrect"] = -1
        b["scroll_axis"] = 0
    for ri, r in enumerate(plan.get("ui_scrollrects") or []):
        for key, axis in (("hbar", 0), ("vbar", 1)):
            bi = int(r.get(key) if r.get(key) is not None else -1)
            if 0 <= bi < len(bars):
                bars[bi]["scrollrect"] = int(ri)
                bars[bi]["scroll_axis"] = int(axis)


def _rewrite_toggle_is_on(text):
    """``arr[i].isOn = v`` / ``toggle.isOn`` → ``Toggle_set/get_isOn``.

    Runs after singleton/ref-array rewrites so ``Instance.toggles[i].isOn``
    is already ``Class_toggles[i].isOn``. Index expr must stay on one
    bracket pair (no DOTALL) so ``equipped[i]; … toggles[x].isOn`` cannot
    merge into one match.
    """
    # Allow calls inside the index (IndexOf(x)) but not `;` / newlines / `]`.
    idx = r"([^\]\n;]*)"
    text = cs2cpp.code_sub(
        r"(\w+)\s*\[%s\]\s*\.\s*isOn\s*=\s*([^;]+);" % idx,
        r"Toggle_set_isOn(\1[\2], (\3));",
        text)
    text = cs2cpp.code_sub(
        r"(?<![\w.])(\w+)\s*\.\s*isOn\s*=\s*([^;]+);",
        r"Toggle_set_isOn(\1, (\2));",
        text)
    text = cs2cpp.code_sub(
        r"(\w+)\s*\[%s\]\s*\.\s*isOn\b" % idx,
        r"Toggle_get_isOn(\1[\2])",
        text)
    text = cs2cpp.code_sub(
        r"(?<![\w.])(\w+)\s*\.\s*isOn\b",
        r"Toggle_get_isOn(\1)",
        text)
    return text


#: Emitted helpers that hand back a `Rect` — a receiver its properties read.
_RECT_VALUE_CALLS = ("RectTransform_GetWorldRect", "RectTransform_get_rect",
                     "Rect_MinMaxRect", "Rect_make")


def _rect_receiver(text, scan, end):
    """The Rect expression ending at *end*, as (start, source), or None."""
    j = end - 1
    while j >= 0 and scan[j] in " \t":
        j -= 1
    if j < 0:
        return None
    if scan[j] == ")":
        depth = 0
        while j >= 0:
            if scan[j] == ")":
                depth += 1
            elif scan[j] == "(":
                depth -= 1
                if depth == 0:
                    break
            j -= 1
        if j < 0:
            return None
    else:
        # A bare name: the last character is its own, not a call's `(`.
        j += 1
    k = j
    while k > 0 and (scan[k - 1].isalnum() or scan[k - 1] == "_"):
        k -= 1
    if k == j:
        return None
    return k, text[k:end]


def _rewrite_recttransform_apis(text, cl, plan):
    """Lower rectTransform.anchoredPosition / sizeDelta / localScale."""
    if not plan.get("live_rt") or not plan.get("go_names"):
        return text
    idn = _c_ident(cl["name"])
    this_go = "_engine_go_of_%s(i)" % idn
    flags = re.DOTALL
    # Receiver: rectTransform / transform / field / field[i] / a.b
    _recv = (
        r"((?:this\s*\.\s*)?rectTransform|(?:this\s*\.\s*)?transform|"
        r"(?:[\w]+(?:\s*\.\s*[\w]+|\s*\[[^\]]+\])*))"
    )

    def _go_of(recv):
        recv = (recv or "").strip()
        if not recv or recv in ("rectTransform", "transform", "this"):
            return this_go
        if re.match(r"(?:this\s*\.\s*)?rectTransform\s*$", recv):
            return this_go
        if re.match(r"(?:this\s*\.\s*)?transform\s*$", recv):
            return this_go
        return recv

    for prop, setter in (
            ("anchoredPosition", "RectTransform_set_anchoredPosition_xy"),
            ("sizeDelta", "RectTransform_set_sizeDelta_xy")):
        def _repl_eq(m, setfn=setter):
            go = _go_of(m.group(1))
            args = _split_call_args(m.group(2))
            if len(args) < 2:
                return m.group(0)
            return "%s(%s, (%s), (%s));" % (setfn, go, args[0], args[1])

        text = cs2cpp.code_sub(
            r"(?<![.\w])%s\s*\.\s*%s\s*=\s*new\s+Vector2\s*\((.*?)\)\s*;"
            % (_recv, prop),
            _repl_eq, text, flags=flags)
        text = cs2cpp.code_sub(
            r"(?<![.\w])%s\s*\.\s*%s\s*=\s*Vector2\.zero\s*;" % (_recv, prop),
            lambda m, setfn=setter: "%s(%s, 0.f, 0.f);" % (
                setfn, _go_of(m.group(1))),
            text)

        def _repl_vec(m, setfn=setter):
            go = _go_of(m.group(1))
            rhs = m.group(2).strip()
            return (
                "%s(%s, Vector2_x(%s), Vector2_y(%s));"
                % (setfn, go, rhs, rhs))

        text = cs2cpp.code_sub(
            r"(?<![.\w])%s\s*\.\s*%s\s*=\s*([^;]+);" % (_recv, prop),
            _repl_vec, text)

    def _repl_scale(m):
        go = _go_of(m.group(1))
        args = _split_call_args(m.group(2))
        if len(args) < 2:
            return m.group(0)
        return "RectTransform_set_localScale_xy(%s, (%s), (%s));" % (
            go, args[0], args[1])

    text = cs2cpp.code_sub(
        r"(?<![.\w])%s\s*\.\s*localScale\s*=\s*new\s+Vector3\s*\((.*?)\)\s*;"
        % _recv,
        _repl_scale, text, flags=flags)
    text = cs2cpp.code_sub(
        r"(?<![.\w])%s\s*\.\s*localScale\s*=\s*new\s+Vector2\s*\((.*?)\)\s*;"
        % _recv,
        _repl_scale, text, flags=flags)

    def _repl_scale_one(m):
        go = _go_of(m.group(1))
        s = m.group(2).strip()
        return "RectTransform_set_localScale_xy(%s, (%s), (%s));" % (go, s, s)

    text = cs2cpp.code_sub(
        r"(?<![.\w])%s\s*\.\s*localScale\s*=\s*Vector3\s*\.\s*one\s*\*\s*([^;]+);"
        % _recv,
        _repl_scale_one, text)

    for prop, gx, gy in (
            ("anchoredPosition",
             "RectTransform_get_anchoredPosition_x",
             "RectTransform_get_anchoredPosition_y"),
            ("sizeDelta",
             "RectTransform_get_sizeDelta_x",
             "RectTransform_get_sizeDelta_y")):
        text = cs2cpp.code_sub(
            r"(?<![.\w])%s\s*\.\s*%s\b(?!\s*[=.])" % (_recv, prop),
            lambda m, x=gx, y=gy: "Vector2_make(%s(%s), %s(%s))" % (
                x, _go_of(m.group(1)), y, _go_of(m.group(1))),
            text)
        text = cs2cpp.code_sub(
            r"(?<![.\w])%s\s*\.\s*%s\s*\.\s*x\b" % (_recv, prop),
            lambda m, x=gx: "%s(%s)" % (x, _go_of(m.group(1))),
            text)
        text = cs2cpp.code_sub(
            r"(?<![.\w])%s\s*\.\s*%s\s*\.\s*y\b" % (_recv, prop),
            lambda m, y=gy: "%s(%s)" % (y, _go_of(m.group(1))),
            text)

    text = cs2cpp.code_sub(
        r"(?<![.\w])%s\s*\.\s*localScale\s*\.\s*x\b" % _recv,
        lambda m: "RectTransform_get_localScale_x(%s)" % _go_of(m.group(1)),
        text)
    text = cs2cpp.code_sub(
        r"(?<![.\w])%s\s*\.\s*localScale\s*\.\s*y\b" % _recv,
        lambda m: "RectTransform_get_localScale_y(%s)" % _go_of(m.group(1)),
        text)
    # Remaining bare rectTransform → this GO (≡ GetComponent<RectTransform>).
    text = cs2cpp.code_sub(
        r"(?<![.\w])(?:this\s*\.\s*)?rectTransform\b",
        this_go, text)
    return text


def _emit_rect_struct(p, want_point_to_normalized=True):
    """UnityEngine.Rect: the struct, and the properties C# reads off one.

    `x`/`y`/`width`/`height` are fields in both languages and need nothing.
    `center`, `size`, `min` and `max` are C# properties, so each is a small
    function here; `center` also assigns, which moves the rect.
    """
    p("/* UnityEngine.Rect — x/y is the min corner (Unity's own layout). */")
    p("typedef struct Rect {")
    p("    float x;")
    p("    float y;")
    p("    float width;")
    p("    float height;")
    p("} Rect;")
    p("static Rect Rect_make(float ax, float ay, float aw, float ah) {")
    p("    Rect r; r.x = ax; r.y = ay; r.width = aw; r.height = ah;")
    p("    return r;")
    p("}")
    p("static Rect Rect_MinMaxRect(float x0, float y0, float x1, float y1) {")
    p("    return Rect_make(x0, y0, x1 - x0, y1 - y0);")
    p("}")
    p("static float Rect_center_x(Rect r) { return r.x + r.width * 0.5f; }")
    p("static float Rect_center_y(Rect r) { return r.y + r.height * 0.5f; }")
    p("static Vector2 Rect_center(Rect r) {")
    p("    return Vector2_make(Rect_center_x(r), Rect_center_y(r));")
    p("}")
    p("static float Rect_size_x(Rect r) { return r.width; }")
    p("static float Rect_size_y(Rect r) { return r.height; }")
    p("static Vector2 Rect_size(Rect r) {")
    p("    return Vector2_make(r.width, r.height);")
    p("}")
    p("static Vector2 Rect_min(Rect r) { return Vector2_make(r.x, r.y); }")
    p("static Vector2 Rect_max(Rect r) {")
    p("    return Vector2_make(r.x + r.width, r.y + r.height);")
    p("}")
    p("/* `rect.center = v` keeps the size and moves the min corner. */")
    p("static void Rect_set_center(Rect *r, Vector2 v) {")
    p("    r->x = Vector2_x(v) - r->width * 0.5f;")
    p("    r->y = Vector2_y(v) - r->height * 0.5f;")
    p("}")
    if want_point_to_normalized:
        p("/* Mathf.InverseLerp — 0 on a degenerate range, clamped [0,1]. */")
        p("static float Mathf_InverseLerp(float a, float b, float v) {")
        p("    float t;")
        p("    if (a == b) return 0.f;")
        p("    t = (v - a) / (b - a);")
        p("    if (t < 0.f) t = 0.f;")
        p("    if (t > 1.f) t = 1.f;")
        p("    return t;")
        p("}")
        p("/* Rect.PointToNormalized(r, p) — the point in the rect's [0,1]. */")
        p("static Vector2 Rect_PointToNormalized(Rect r, Vector2 p) {")
        p("    return Vector2_make(")
        p("        Mathf_InverseLerp(r.x, r.x + r.width, p.x),")
        p("        Mathf_InverseLerp(r.y, r.y + r.height, p.y));")
        p("}")
    p("")


_UNITY_API_RECT = [
    _B("Rect.PointToNormalized", "Rect_PointToNormalized", namespaces=_UE),
    _B("Rect.MinMaxRect", "Rect_MinMaxRect", namespaces=_UE),
    _B("Camera.main.ScreenToWorldPoint", "Camera_main_ScreenToWorldPoint",
       namespaces=_UE),
    _B("Mouse.current.position.ReadValue", "Mouse_current_position",
       namespaces=("UnityEngine.InputSystem",)),
]
