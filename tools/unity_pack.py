#!/usr/bin/env python3
"""unity_pack -- emit a packed engine.c + data.c from a Unity-subset project.

See UNITY_PACK.md. Walks scripts and scenes, keeps only the Unity API that
is called, and lays objects out as small as the data allows: drop z in 2D,
float16 for static backgrounds, bitfields for small ints, and uint8_t
indices instead of pointers when a class is bounded (hand-placed, never
spawned, N ≤ 256).

Does not invent scene assets (ParticleSystem pools, AnimationCurves,
InputAction maps). Runtime `AddComponent<T>` is supported for packed
builtins and authored MonoBehaviours. Types with
`DisallowMultipleComponent` (all packed builtins; scripts that declare
the attribute) refuse a second add and print Unity's error; others
GetOrAdd into a pre-sized pool.

    python3 tools/unity_pack.py <project>
    python3 tools/unity_pack.py <project> -o <outdir>

Default output is $TMPDIR/<project-folder>/<productName>[.exe], a linked
player (GLFW window when glfw3 is present, otherwise the headless host).
"""

from __future__ import annotations

import os
import re
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cs2cpp as cs2cpp  # noqa: E402


class PackError(Exception):
    def __init__(self, message):
        self.message = message
        Exception.__init__(self, message)


def _assets_rel_path(path):
    """Unity-style path: `Assets/...` when under an Assets tree."""
    norm = path.replace("\\", "/")
    i = norm.find("/Assets/")
    if i >= 0:
        return norm[i + 1:]
    if norm.startswith("Assets/"):
        return norm
    return os.path.basename(path) if path else "<cs>"


def _cs_diag(path, text, idx, code, message):
    """Unity/csc diagnostic: `Assets/.../File.cs(line,col): error CSxxxx: …`."""
    line = text.count("\n", 0, idx) + 1
    col = idx - (text.rfind("\n", 0, idx) + 1) + 1
    return "%s(%d,%d): error %s: %s" % (
        _assets_rel_path(path), line, col, code, message)


def _raise_cs(path, text, idx, code, message):
    raise PackError(_cs_diag(path, text, idx, code, message))


def _raise_cs_at_site(site, body_idx, code, message):
    """CS diagnostic using method-body offset + emit site (path / file_text)."""
    path = site.get("path") or "<cs>"
    ft = site.get("file_text") or ""
    if not ft:
        raise PackError("%s(1,1): error %s: %s" % (
            _assets_rel_path(path), code, message))
    abs_i = int(site.get("body_abs") or 0) + int(body_idx or 0)
    _raise_cs(path, ft, abs_i, code, message)


def _raise_unknown_component_type(t, analyses, ops=("AddComponent", "GetComponent")):
    """Unknown / refused component type → Unity CS0246 at the type token."""
    analyses = analyses or []
    for a in analyses:
        path = a.get("path") or ""
        text = None
        for c in a.get("classes") or []:
            if c.get("file_text") is not None:
                text = c["file_text"]
                break
        if text is None and path and os.path.isfile(path):
            text = _read(path)
        if not text:
            continue
        scan = cs2cpp._blank(text)
        for op in ops:
            m = re.search(
                r"%s\s*<\s*(?:[\w.]*\.)?(%s)\s*>" % (op, re.escape(t)),
                scan)
            if m:
                _raise_cs(path, text, m.start(1), "CS0246", _CS0246 % t)
    if analyses:
        path = analyses[0].get("path") or "<cs>"
        raise PackError("%s(1,1): error CS0246: %s" % (
            _assets_rel_path(path), _CS0246 % t))
    raise PackError("<cs>(1,1): error CS0246: %s" % (_CS0246 % t))


# System.IO.File members we emit. Others → CS0117 (File is in scope via using).
_FILE_SUPPORTED = frozenset({
    "WriteAllText", "AppendAllText", "WriteAllBytes", "ReadAllBytes",
    "Exists", "Delete", "CreateText", "OpenText", "Copy",
})

# UnityEngine.Application members we emit. Others → CS0117.
_APPLICATION_SUPPORTED = frozenset({
    "dataPath", "persistentDataPath", "isEditor", "isPlaying", "OpenURL",
    "productName", "Quit",
})

# UnityEngine.Quaternion members we emit. Others → CS0117 (in scope via UnityEngine).
_QUATERNION_SUPPORTED = frozenset({
    "Euler", "identity", "LookRotation", "Slerp", "Inverse", "Angle",
    "RotateTowards",
})

# MonoBehaviour.transform members we lower. Others → CS1061 on Transform
# (transform itself is always in scope; blame the missing member).
_TRANSFORM_SUPPORTED = frozenset({
    "position", "Rotate", "LookAt", "eulerAngles", "rotation", "Find",
    "localScale", "parent", "gameObject", "SetParent", "GetSiblingIndex",
    "worldToLocalMatrix", "localToWorldMatrix",
    "localPosition", "localRotation",
    "TransformPoint",
})


_FILE_MEMBER_CALL = re.compile(
    r"System\.IO\.File\.(\w+)\s*\("
    r"|(?<![\w.])File\.(\w+)\s*\(")


def _check_file_api(path, text, scan):
    """Unsupported File.Member with System.IO in scope → Unity CS0117."""
    has_io = bool(re.search(r"using\s+System\.IO\b", scan))
    for m in _FILE_MEMBER_CALL.finditer(scan):
        method = m.group(1) or m.group(2)
        if method in _FILE_SUPPORTED:
            continue
        is_fqn = m.group(1) is not None
        if not is_fqn and not has_io:
            continue  # bare File without using — not CS0117
        method_idx = m.start(1) if is_fqn else m.start(2)
        _raise_cs(
            path, text, method_idx, "CS0117",
            "'File' does not contain a definition for '%s'" % method)


def _check_application_api(path, text, scan):
    """Unsupported Application.Member with UnityEngine in scope → CS0117.

    Must not match inside ``EditorApplication`` / other *Application types.
    """
    has_ue = bool(re.search(r"using\s+UnityEngine\b", scan))
    for m in re.finditer(
            r"(?<![\w])(?:UnityEngine\.)?Application\.(\w+)\b", scan):
        member = m.group(1)
        if member in _APPLICATION_SUPPORTED:
            continue
        is_fqn = m.group(0).startswith("UnityEngine.")
        if not is_fqn and not has_ue:
            continue
        member_idx = m.start(1)
        _raise_cs(
            path, text, member_idx, "CS0117",
            "'Application' does not contain a definition for '%s'" % member)


def _check_quaternion_api(path, text, scan):
    """Unsupported Quaternion.Member with UnityEngine in scope → Unity CS0117."""
    has_ue = bool(re.search(r"using\s+UnityEngine\b", scan))
    for m in re.finditer(r"(?:UnityEngine\.)?Quaternion\.(\w+)\b", scan):
        member = m.group(1)
        if member in _QUATERNION_SUPPORTED:
            continue
        is_fqn = m.group(0).startswith("UnityEngine.")
        if not is_fqn and not has_ue:
            continue
        member_idx = m.start(1)
        _raise_cs(
            path, text, member_idx, "CS0117",
            "'Quaternion' does not contain a definition for '%s'" % member)


def _check_transform_api(path, text, scan):
    """Unsupported MonoBehaviour.transform.Member → Unity CS1061.

    `transform` is always a Transform (never an undeclared identifier).
    Skips `Camera.main.transform` / other `*.transform` (leading `.`).
    """
    for m in re.finditer(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*(\w+)", scan):
        member = m.group(1)
        if member in _TRANSFORM_SUPPORTED:
            continue
        member_idx = m.start(1)
        _raise_cs(
            path, text, member_idx, "CS1061",
            "'Transform' does not contain a definition for '%s' and no "
            "accessible extension method '%s' accepting a first argument of "
            "type 'Transform' could be found (are you missing a using "
            "directive or an assembly reference?)"
            % (member, member))


_CS0246 = (
    "The type or namespace name '%s' could not be found (are you missing a "
    "using directive or an assembly reference?)"
)
_CS0234 = (
    "The type or namespace name '%s' does not exist in the namespace '%s' "
    "(are you missing an assembly reference?)"
)


def _blank_unity_editor_regions(text):
    """Blank ``#if UNITY_EDITOR`` … ``#endif`` bodies (player pack ignores them).

    Nested ``#if`` depth inside an editor region is tracked so OnValidate and
    other editor-only APIs never reach refuse checks or method lowering.
    """
    lines = text.split("\n")
    out = []
    depth = 0
    for line in lines:
        s = line.lstrip()
        if s.startswith("#"):
            low = s.lower()
            if re.match(r"#if\b", low) and re.search(r"\bunity_editor\b", low):
                depth += 1
                out.append(" " * len(line))
                continue
            if depth > 0:
                if low.startswith("#endif"):
                    depth -= 1
                elif low.startswith("#if"):
                    depth += 1
                out.append(" " * len(line))
                continue
            out.append(line)
            continue
        if depth > 0:
            out.append(" " * len(line))
        else:
            out.append(line)
    return "\n".join(out)


def _check_refused_api(path, text, scan):
    """Packed-subset refusals → Unity/csc diagnostics at the use site.

    ``using UnityEngine.UI`` is allowed (authored Image/Button fields). Invent
    (AddComponent<Canvas>, ForceUpdateCanvases, typeof(Canvas) spawn) is not.
    """
    # Scripted Canvas invent — not authored !u!223.
    m = re.search(
            r"AddComponent\s*<\s*(?:UnityEngine\.)?(Canvas)\s*>", scan)
    if m:
        _raise_cs(path, text, m.start(1), "CS0246", _CS0246 % "Canvas")
    m = re.search(r"typeof\s*\(\s*(Canvas)\s*\)", scan)
    if m:
        _raise_cs(path, text, m.start(1), "CS0246", _CS0246 % "Canvas")
    m = re.search(r"Canvas\.(ForceUpdateCanvases)\b", scan)
    if m:
        _raise_cs(
            path, text, m.start(1), "CS0117",
            "'Canvas' does not contain a definition for 'ForceUpdateCanvases'")
    m = re.search(r"(?<![\w.])InputAction\b", scan)
    if m:
        _raise_cs(path, text, m.start(), "CS0246", _CS0246 % "InputAction")
    # Member forms analyze maps to refused keys (Emit / Evaluate / current).
    m = re.search(r"ParticleSystem\.(Emit)\b", scan)
    if m:
        _raise_cs(
            path, text, m.start(1), "CS0117",
            "'ParticleSystem' does not contain a definition for 'Emit'")
    m = re.search(r"AnimationCurve\.(Evaluate)\b", scan)
    if m:
        _raise_cs(
            path, text, m.start(1), "CS0117",
            "'AnimationCurve' does not contain a definition for 'Evaluate'")
    m = re.search(r"(?<![\w.])Gamepad\.(current)\b", scan)
    if m:
        _raise_cs(
            path, text, m.start(1), "CS0117",
            "'Gamepad' does not contain a definition for 'current'")
    # Keyboard.current without UnityEngine.InputSystem in scope.
    has_input_system = bool(
        re.search(r"using\s+UnityEngine\.InputSystem\b", scan)
        or re.search(r"UnityEngine\.InputSystem\.Keyboard\b", scan)
    )
    if (not has_input_system
            and (re.search(r"(?<![\w.])Keyboard\.current\b", scan)
                 or _KEYBOARD_KEY.search(scan))):
        km = re.search(r"(?<![\w.])Keyboard\b", scan)
        if km:
            _raise_cs(path, text, km.start(), "CS0246",
                      _CS0246 % "Keyboard")
    # Bare Console.WriteLine without using System / FQN.
    has_system = bool(re.search(r"using\s+System\b", scan))
    if (not has_system
            and not re.search(r"System\.Console\.WriteLine\s*\(", scan)
            and re.search(r"(?<![\w.])Console\.WriteLine\s*\(", scan)):
        m = re.search(r"(?<![\w.])Console\b", scan)
        if m:
            _raise_cs(path, text, m.start(), "CS0246", _CS0246 % "Console")
    # AddComponent<T> for invent-refused builtins (Canvas / …).
    for m in re.finditer(
            r"AddComponent\s*<\s*(?:UnityEngine\.)?(\w+)\s*>", scan):
        t = m.group(1)
        if t in _REFUSED_ADDCOMPONENT:
            _raise_cs(path, text, m.start(1), "CS0246", _CS0246 % t)


def _check_csharp_lex(path, text):
    """Refuse spellings Unity/csc reject before any rewrite.

    C# real-literals need digits after `.` (`0.0f`) or a bare suffix (`0f`).
    C++-style `0.f` lexes as integer `0`, member access `.`, identifier `f`
    → CS1061. Catch it here so diagnostics stay against C# source.
    """
    text = _blank_unity_editor_regions(text)
    scan = cs2cpp._blank(text)
    for m in re.finditer(r"(?<![\w.])\d+\.([fFdDmM])\b", scan):
        suffix = m.group(1)
        _raise_cs(
            path, text, m.start(1), "CS1061",
            "'int' does not contain a definition for '%s' and no accessible "
            "extension method '%s' accepting a first argument of type 'int' "
            "could be found (are you missing a using directive or an "
            "assembly reference?)"
            % (suffix, suffix))
    # transform.position is Vector3; += Vector2 is ambiguous (CS0034).
    # Assignment `= new Vector2(...)` is fine via Vector2→Vector3 implicit.
    for m in re.finditer(
            r"transform\.position\s*(?:\+=|-=)\s*new\s+Vector2\b", scan):
        _raise_cs(
            path, text, m.start(), "CS0034",
            "Operator '%s' is ambiguous on operands of type 'Vector3' and "
            "'Vector2'"
            % ("+=" if "+=" in m.group(0) else "-="))
    _check_file_api(path, text, scan)
    _check_application_api(path, text, scan)
    _check_quaternion_api(path, text, scan)
    _check_transform_api(path, text, scan)
    _check_refused_api(path, text, scan)


# Built-in Unity components AddComponent may create at runtime.
_ADDABLE_BUILTINS = frozenset((
    "Camera",
    "Light",
    "SpriteRenderer",
    "Rigidbody2D",
    "Rigidbody",
    "BoxCollider2D",
    "CircleCollider2D",
    "BoxCollider",
    "SphereCollider",
    "Animation",
    "Animator",
    "AudioSource",
))

# Unity marks these with [DisallowMultipleComponent] — a second AddComponent
# logs an error and returns null instead of returning the existing instance.
# AudioSource is omitted: Unity allows several AudioSources on one GameObject.
_DISALLOW_MULTIPLE_BUILTINS = frozenset(
    t for t in _ADDABLE_BUILTINS if t != "AudioSource")

# Types that still require inventing assets / systems — AddComponent refused.
_REFUSED_ADDCOMPONENT = frozenset((
    "ParticleSystem",
    "Canvas",
))

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


def _progress(msg):
    """Incremental status for long packs (large scenes / many PNGs)."""
    sys.stderr.write("unity_pack: %s\n" % msg)
    sys.stderr.flush()


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
    "Mathf.Sign": (
        "static float Mathf_Sign(float f) {\n"
        "    if (f < 0.f) return -1.f; if (f > 0.f) return 1.f; return 0.f;\n}"
    ),
    "Time.deltaTime": None,  # globals in data.c — host can poke
    "Time.time": None,
    "Time.fixedDeltaTime": None,
    "Physics2D.gravity": None,
    "Physics.gravity": None,
    "Rigidbody2D": True,
    "Rigidbody": True,
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
    # Path to Assets/ (Editor) — baked from the packed project root.
    "Application.dataPath": True,
    "Application.persistentDataPath": True,
    "Application.isEditor": True,
    "Application.isPlaying": True,
    "Application.OpenURL": True,
    "Application.productName": True,
    "Application.Quit": True,
    "File.WriteAllText": True,
    "File.AppendAllText": True,
    "File.WriteAllBytes": True,
    "File.ReadAllBytes": True,
    "File.Exists": True,
    "File.Delete": True,
    "File.CreateText": True,
    "File.OpenText": True,
    "File.Copy": True,
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
    "Canvas": (
        "Scripted Canvas invent (AddComponent / typeof / ForceUpdateCanvases) "
        "is refused — author a !u!223 Canvas + Image in the scene."
    ),
}

_SPAWN = re.compile(
    r"(?<![\w.])(Instantiate|Object\.Instantiate|"
    r"GameObject\.Instantiate|new\s+GameObject)\b"
)
# Destroy(gameObject) does not allocate — not a spawn.
_DESTROY = re.compile(
    r"(?<![\w.])(?:Object\.)?Destroy\s*\("
)
_VEC3Z = re.compile(r"\.(z)\b|Vector3|Quaternion")
_UNITY_API = re.compile(
    r"(?:AddComponent\s*<\s*[\w.]+\s*>|"
    r"(?<![\w])(?:Mathf\.(?:Abs|Min|Max|Clamp|Lerp|Sin|Cos|Sign)|"
    r"Time\.(?:deltaTime|time|fixedDeltaTime)|"
    r"Screen\.(?:width|height)|"
    r"(?<![\w])Application\.dataPath|"
    r"(?<![\w])Application\.persistentDataPath|"
    r"(?<![\w])Application\.isEditor|"
    r"(?<![\w])Application\.isPlaying|"
    r"(?<![\w])Application\.OpenURL|"
    r"(?<![\w])Application\.productName|"
    r"(?<![\w])Application\.Quit|"
    r"File\.(?:WriteAllText|AppendAllText|WriteAllBytes|ReadAllBytes|"
    r"Exists|Delete|CreateText|OpenText|Copy)|"
    r"Input\.(?:GetAxis|GetButton|GetKey)|"
    r"RenderSettings\.ambientLight|Camera\.main|"
    r"transform\.position|Physics2D\.gravity|Physics\.gravity|"
    r"Rigidbody2D|Rigidbody|"
    r"ParticleSystem\.Emit|"
    r"AnimationCurve\.Evaluate|"
    r"InputAction|Keyboard\.current|Gamepad\.current|"
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


def player_screen(root):
    """defaultScreenWidth / Height from ProjectSettings (Unity Player Settings).

    Missing keys → Unity standalone defaults 1024×768. Values ≤0 are clamped
    to 1 so hosts never create a zero-size window.
    """
    width, height, _fs, _native, _max = player_display(root)
    return width, height


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


def _apply_sprite_sorting(objects, sorting_layers):
    """Resolve SpriteRenderer sorting_layer index from TagManager uniqueIDs."""
    id_to_idx = {}
    for i, L in enumerate(sorting_layers or []):
        id_to_idx[int(L["unique_id"])] = i
    for o in objects:
        sp = o.get("sprite")
        if not sp:
            continue
        lid = int(sp.get("sorting_layer_id") or 0)
        if lid in id_to_idx:
            sp["sorting_layer"] = id_to_idx[lid]
        else:
            # Fall back to authored m_SortingLayer index (clamped).
            idx = int(sp.get("sorting_layer_yaml") or 0)
            n = len(sorting_layers) if sorting_layers else 1
            if idx < 0:
                idx = 0
            if idx >= n:
                idx = n - 1
            sp["sorting_layer"] = idx
        sp["sorting_order"] = int(sp.get("sorting_order") or 0)


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


def unity_persistent_data_path(company, product, home=None):
    """Host path matching Unity's Application.persistentDataPath."""
    if home is None:
        home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(
            home, "Library", "Application Support", company, product)
    if sys.platform.startswith("win"):
        base = os.environ.get("USERPROFILE") or home
        return os.path.join(base, "AppData", "LocalLow", company, product)
    return os.path.join(home, ".config", "unity3d", company, product)


def _read(path):
    with open(path) as f:
        return f.read()


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


def _is_player_csharp(root, path):
    """Runtime C# under Assets/ — excludes Unity Editor/ assemblies.

    Matches player builds: only Assets scripts pack as MonoBehaviours;
    any path segment named exactly ``Editor`` is editor-only (Unity).
    """
    if not path.lower().endswith(".cs"):
        return False
    if not _path_under_assets(root, path):
        return False
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    parts = rel.replace("\\", "/").split("/")
    return "Editor" not in parts


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


def _pixels_per_unit(asset_path):
    """Unity TextureImporter `spritePixelsToUnits` (Sprite.pixelsPerUnit).

    Default 100 matches Unity when the .meta omits the field.
    """
    meta = asset_path + ".meta"
    try:
        text = _read(meta)
    except IOError:
        return 100.0
    m = re.search(r"(?m)^\s*spritePixelsToUnits:\s*([0-9.]+)\s*$", text)
    if not m:
        return 100.0
    v = float(m.group(1))
    return v if v > 0.0 else 100.0


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


# Unity defaults when Collider/Rigidbody m_Material is {fileID: 0}.
_DEFAULT_MAT2D = {
    "friction": 0.4,
    "bounciness": 0.0,
    "friction_combine": 0,  # Average
    "bounce_combine": 0,
}
_DEFAULT_MAT3D = {
    "dynamic_friction": 0.6,
    "static_friction": 0.6,
    "bounciness": 0.0,
    "friction_combine": 0,
    "bounce_combine": 0,
}


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


def _parse_physics_material2d(text):
    fr = re.search(r"(?m)^\s+friction:\s*([0-9.eE+-]+)", text)
    bn = re.search(r"(?m)^\s+bounciness:\s*([0-9.eE+-]+)", text)
    fc = re.search(r"(?m)^\s+m_FrictionCombine:\s*(\d+)", text)
    bc = re.search(r"(?m)^\s+m_BounceCombine:\s*(\d+)", text)
    return {
        "friction": float(fr.group(1)) if fr else 0.4,
        "bounciness": float(bn.group(1)) if bn else 0.0,
        "friction_combine": int(fc.group(1)) if fc else 0,
        "bounce_combine": int(bc.group(1)) if bc else 0,
    }


def _parse_physic_material3d(text):
    df = re.search(r"(?m)^\s+dynamicFriction:\s*([0-9.eE+-]+)", text)
    sf = re.search(r"(?m)^\s+staticFriction:\s*([0-9.eE+-]+)", text)
    bn = re.search(r"(?m)^\s+bounciness:\s*([0-9.eE+-]+)", text)
    fc = re.search(r"(?m)^\s+frictionCombine:\s*(\d+)", text)
    bc = re.search(r"(?m)^\s+bounceCombine:\s*(\d+)", text)
    return {
        "dynamic_friction": float(df.group(1)) if df else 0.6,
        "static_friction": float(sf.group(1)) if sf else 0.6,
        "bounciness": float(bn.group(1)) if bn else 0.0,
        "friction_combine": int(fc.group(1)) if fc else 0,
        "bounce_combine": int(bc.group(1)) if bc else 0,
    }


def _load_physics_materials(asset_guids):
    """guid → PhysicsMaterial2D / PhysicMaterial from project assets."""
    mats2d = {}
    mats3d = {}
    for guid, path in (asset_guids or {}).items():
        low = path.lower()
        try:
            if low.endswith(".physicsmaterial2d"):
                mats2d[guid.lower()] = _parse_physics_material2d(_read(path))
            elif low.endswith(".physicmaterial"):
                mats3d[guid.lower()] = _parse_physic_material3d(_read(path))
        except (IOError, OSError):
            continue
    return mats2d, mats3d


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


# ---------------------------------------------------------------------------
# AnimationClip / AnimatorController (authored assets)
# ---------------------------------------------------------------------------

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


def _attach_sprite_textures(objects, asset_guids):
    """Load PNG pixels for each SpriteRenderer that references a project sprite.

    World half-extents follow Unity: (pixels / pixelsPerUnit) * scale / 2.
    PNG decode is cached by path so shared sprites are not re-decoded.
    """
    todo = [o for o in objects if o.get("sprite")]
    n = len(todo)
    cache = {}  # path -> (w, h, rgba, ppu) or None if unloadable
    if n:
        _progress("loading sprites for %d SpriteRenderer(s)" % n)
    for i, o in enumerate(todo):
        if n >= 8 and ((i + 1) % 100 == 0 or i + 1 == n):
            _progress("  sprites %d/%d (%d unique PNG(s))" % (
                i + 1, n, len(cache)))
        sp = o.get("sprite")
        if not sp:
            continue
        if sp.get("builtin") or "tex_rgba" in sp:
            if "a" not in sp:
                sp["a"] = 1.0
            continue
        path = asset_guids.get(sp.get("sprite_guid") or "")
        if not path or not path.lower().endswith(".png"):
            o["sprite"] = None
            continue
        if path not in cache:
            try:
                w, h, rgba = _load_png_rgba(path)
                cache[path] = (w, h, rgba, _pixels_per_unit(path))
            except (PackError, IOError):
                cache[path] = None
        hit = cache[path]
        if hit is None:
            o["sprite"] = None
            continue
        w, h, rgba, ppu = hit
        sx = abs(float(sp.get("scale_x", 1.0)))
        sy = abs(float(sp.get("scale_y", 1.0)))
        sp["tex_path"] = path
        sp["tex_w"] = w
        sp["tex_h"] = h
        sp["tex_rgba"] = rgba
        sp["pixels_per_unit"] = ppu
        if sp.get("source") not in ("ui", "ui_tmp"):
            sp["half_w"] = (float(w) / ppu) * sx * 0.5
            sp["half_h"] = (float(h) / ppu) * sy * 0.5
        if "a" not in sp:
            sp["a"] = 1.0


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
                "ppu": float(sp.get("pixels_per_unit") or 100.0),
            })
        sp["tex_id"] = by_guid[g]
    return textures


def _ensure_texture_guids(textures, guids, asset_guids):
    """Load PNGs for animation-only sprite guids into the texture table.

    Idle.anim swaps to Eyes Closed which may not be any SpriteRenderer's
    initial m_Sprite — still must pack those texels.
    """
    by_guid = {t["guid"]: i for i, t in enumerate(textures)}
    cache = {}
    for raw in guids or []:
        g = (raw or "").lower()
        if not g or g in by_guid:
            continue
        path = (asset_guids or {}).get(g)
        if not path or not path.lower().endswith(".png"):
            continue
        if path not in cache:
            try:
                w, h, rgba = _load_png_rgba(path)
                cache[path] = (w, h, rgba, _pixels_per_unit(path))
            except (PackError, IOError):
                cache[path] = None
        hit = cache[path]
        if hit is None:
            continue
        w, h, rgba, ppu = hit
        by_guid[g] = len(textures)
        textures.append({
            "guid": g,
            "path": path,
            "w": w,
            "h": h,
            "rgba": rgba,
            "ppu": float(ppu),
        })
    return by_guid


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


# Builtin uGUI Image / Button MonoBehaviour script guids (UnityEngine.UI.dll).
_IMAGE_SCRIPT_GUID = "fe87c0e1cc204ed48ad3b37840f39efc"
_BUTTON_SCRIPT_GUID = "4e29b1a8efbd4b44bb3f3716e73f07ff"
# TextMeshProUGUI (com.unity.ugui / Unity.TextMeshPro).
_TMP_UGUI_SCRIPT_GUID = "f4688fdb7df04437aeb418b961361dc5"
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


def _is_ui_image_mb(block, guid):
    if (guid or "").lower() == _IMAGE_SCRIPT_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bImage\s*$", block))


def _is_ui_button_mb(block, guid):
    if (guid or "").lower() == _BUTTON_SCRIPT_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bButton\s*$", block))


def _is_ui_tmp_mb(block, guid):
    if (guid or "").lower() == _TMP_UGUI_SCRIPT_GUID:
        return True
    return bool(re.search(
        r"(?m)^\s+m_EditorClassIdentifier:.*\bTextMeshProUGUI\s*$", block))


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
        "enabled": int(en.group(1)) if en else 1,
    }


def _parse_ui_button(block):
    """Authored uGUI Button → interactable, ColorBlock, persistent onClick."""
    en = re.search(r"(?m)^\s+m_Interactable:\s*(\d+)", block)

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
            calls.append({
                "target_go": str(tid),
                "method": cm.group(2),
                "mode": int(cm.group(3)),
                "bool_arg": int(cm.group(4)),
            })
    return {
        "interactable": int(en.group(1)) if en else 1,
        "colors": colors,
        "onclick": calls,
    }


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


def _ui_screen_rect(o, by_xf, screen_w, screen_h, cache):
    """Pixel rect (cx, cy, w, h) in screen space for a RectTransform object."""
    key = str(o.get("xf_id") or id(o))
    if key in cache:
        return cache[key]
    sw = float(screen_w)
    sh = float(screen_h)
    # Canvas root pixel size is the screen (Overlay / Screen Space Camera),
    # not the serialized anchors (often 0,0 with sizeDelta 0).
    if o.get("canvas"):
        cache[key] = (sw * 0.5, sh * 0.5, sw, sh)
        return cache[key]
    fid = o.get("father_id")
    parent = by_xf.get(str(fid)) if fid else None
    if parent is not None and (
            parent.get("rect") is not None or parent.get("canvas")):
        pcx, pcy, pw, ph = _ui_screen_rect(
            parent, by_xf, screen_w, screen_h, cache)
        plx = pcx - pw * 0.5
        ply = pcy - ph * 0.5
    else:
        # Root under Canvas / missing parent → full screen.
        plx, ply, pw, ph = 0.0, 0.0, sw, sh
    rect = o.get("rect") or {}
    amin = rect.get("anchor_min") or (0.5, 0.5)
    amax = rect.get("anchor_max") or (0.5, 0.5)
    apos = rect.get("anchored_position") or (0.0, 0.0)
    size = rect.get("size_delta") or (100.0, 100.0)
    pivot = rect.get("pivot") or (0.5, 0.5)
    lcx, lcy, rw, rh = _rect_pivot_center(
        pw, ph, amin, amax, apos, size, pivot)
    cx = plx + lcx
    cy = ply + lcy
    cache[key] = (cx, cy, abs(rw), abs(rh))
    return cache[key]


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


def _rasterize_tmp_text(font, text, font_size, color, box_w, box_h,
                        h_align, v_align):
    """Bake plain TMP string into an RGBA bitmap (y=0 bottom, OpenGL)."""
    bw = max(1, int(round(float(box_w))))
    bh = max(1, int(round(float(box_h))))
    out = bytearray(bw * bh * 4)
    if not font or not text:
        return bw, bh, bytes(out)
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
        pen_x = float(bw) - total_w
    elif h_align & 2:
        pen_x = (float(bw) - total_w) * 0.5
    else:
        pen_x = 0.0
    # Vertical: 256 top, 512 middle, 1024 bottom.
    if v_align & 1024:
        baseline = -descent
    elif v_align & 512:
        baseline = (float(bh) - visual_h) * 0.5 - descent
    else:
        baseline = float(bh) - ascent
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
    return bw, bh, bytes(out)


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
    """Stretch source into dst (Simple Image.type)."""
    dw = max(1, int(round(float(dst_w))))
    dh = max(1, int(round(float(dst_h))))
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


def _sprite_border_from_meta(path):
    """PNG .meta spriteBorder {x,y,z,w} → (left, bottom, right, top)."""
    meta = path + ".meta"
    if not os.path.isfile(meta):
        return (0.0, 0.0, 0.0, 0.0)
    text = _read(meta)
    m = re.search(
        r"spriteBorder:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
        r"\s*z:\s*([^,}]+),\s*w:\s*([^}]+)\}",
        text)
    if not m:
        return (0.0, 0.0, 0.0, 0.0)
    return (float(m.group(1)), float(m.group(2)),
            float(m.group(3)), float(m.group(4)))


def _bake_ui_images(objects, cameras, screen_w, screen_h, asset_guids=None):
    """Resolve authored uGUI Image / TextMeshProUGUI → world sprites.

    Screen Space Overlay (0) and Screen Space Camera (1): map canvas pixels to
    the main ortho camera frustum. World Space (2) is not supported yet.
    Project PNG sprites and Unity builtin UISprites draw; Image.type Sliced
    9-slices with sprite borders (UISprite corners stay fixed). Empty m_Sprite
    is skipped (no invent). TMP needs an authored font asset with atlas +
    glyph tables.
    """
    by_xf = {}
    for o in objects:
        xid = o.get("xf_id")
        if xid:
            by_xf[str(xid)] = o
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
        canvas = None
        fid = o.get("father_id")
        guard = 0
        while fid and guard < 64:
            guard += 1
            parent = by_xf.get(str(fid))
            if not parent:
                break
            if parent.get("canvas"):
                canvas = parent["canvas"]
                break
            fid = parent.get("father_id")
        if canvas is None:
            canvas = {"render_mode": 0, "sorting_layer_id": 0,
                      "sorting_order": 0, "enabled": 1}
        return canvas

    def _apply_layout(o, cx, cy, rw, rh, canvas, source, color, extra=None):
        wx = cam_x + (cx / float(sw) - 0.5) * world_w
        wy = cam_y + (cy / float(sh) - 0.5) * world_h
        o["pos"] = (wx, wy, float(o["pos"][2]) if o.get("pos") else 0.0)
        o["ui_hit"] = {
            "cx": float(cx), "cy": float(cy),
            "hw": abs(float(rw)) * 0.5, "hh": abs(float(rh)) * 0.5,
            "ncx": float(cx) / float(sw),
            "ncy": float(cy) / float(sh),
            "nhw": abs(float(rw)) * 0.5 / float(sw),
            "nhh": abs(float(rh)) * 0.5 / float(sh),
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
            "scale_x": 1.0,
            "scale_y": 1.0,
            "cos_z": 1.0,
            "sin_z": 0.0,
            "m00": 1.0,
            "m01": 0.0,
            "m10": 0.0,
            "m11": 1.0,
            "half_w": abs(rw) * px_w * 0.5,
            "half_h": abs(rh) * px_h * 0.5,
            "ncx": float(cx) / float(sw),
            "ncy": float(cy) / float(sh),
            "nhw": abs(float(rw)) * 0.5 / float(sw),
            "nhh": abs(float(rh)) * 0.5 / float(sh),
        }
        if extra:
            sp.update(extra)
        o["sprite"] = sp

    for o in objects:
        ui = o.get("ui_image")
        if not ui or not ui.get("has_sprite"):
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
        extra = {
            "builtin": builtin,
            "sprite_file_id": int(ui.get("sprite_file_id") or 0),
            "sprite_guid": ("builtin:uisprite" if builtin
                            else ui.get("sprite_guid")),
            "image_type": img_type,
        }
        if builtin:
            src_w, src_h, src_rgba, border = _builtin_uisprite()
            tex_path = "<builtin:UISprite>"
        else:
            path = asset_guids.get(ui.get("sprite_guid") or "")
            if not path or not path.lower().endswith(".png"):
                continue
            if path not in png_cache:
                try:
                    png_cache[path] = _load_png_rgba(path)
                except Exception:
                    png_cache[path] = None
            loaded = png_cache[path]
            if not loaded:
                continue
            src_w, src_h, src_rgba = loaded
            border = _sprite_border_from_meta(path)
            tex_path = path
        # Sliced (1): 9-slice. Simple (0) / other: stretch to rect.
        # Bake to rect size so one textured quad matches uGUI mesh.
        if img_type == 1 and any(b > 0 for b in border):
            tw, th, rgba = _bake_sliced_rgba(
                src_rgba, src_w, src_h, border, rw, rh, ppu_mul)
        else:
            tw, th, rgba = _bake_stretched_rgba(
                src_rgba, src_w, src_h, rw, rh)
        extra["tex_path"] = tex_path
        extra["tex_w"] = tw
        extra["tex_h"] = th
        extra["tex_rgba"] = rgba
        extra["pixels_per_unit"] = 100.0
        extra["border"] = border
        _apply_layout(
            o, cx, cy, rw, rh, canvas, "ui",
            (ui.get("r", 1.0), ui.get("g", 1.0),
             ui.get("b", 1.0), ui.get("a", 1.0)),
            extra)

    for o in objects:
        tmp = o.get("ui_tmp")
        if not tmp or not tmp.get("has_font") or not int(tmp.get("enabled", 1)):
            continue
        if not (tmp.get("text") or ""):
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
        tw, th, rgba = _rasterize_tmp_text(
            font, tmp["text"], float(tmp.get("font_size") or 14.0),
            (1.0, 1.0, 1.0, 1.0),  # color via sprite tint (m_fontColor)
            rw, rh,
            int(tmp.get("h_align") or 1),
            int(tmp.get("v_align") or 256))
        bake_guid = "tmpbake:%s:%s" % (
            o.get("go_id") or o.get("name") or "tmp",
            tmp.get("font_guid") or "")
        _apply_layout(
            o, cx, cy, rw, rh, canvas, "ui_tmp",
            (tmp.get("r", 1.0), tmp.get("g", 1.0),
             tmp.get("b", 1.0), tmp.get("a", 1.0)),
            {
                "builtin": False,
                "sprite_file_id": 0,
                "sprite_guid": bake_guid,
                "tex_path": "<tmp:%s>" % (tmp.get("text") or ""),
                "tex_w": tw,
                "tex_h": th,
                "tex_rgba": rgba,
                "pixels_per_unit": 100.0,
            })


# ---------------------------------------------------------------------------
# Scene importers
# ---------------------------------------------------------------------------

def parse_unity_yaml(text, guid_to_script=None, asset_guids=None):
    """A Unity .unity YAML subset: GameObject + Transform + MonoBehaviour.

    Also imports authored Camera (!u!20), SpriteRenderer (!u!212), Canvas
    (!u!223), uGUI Image / Button (builtin MB), RectTransform anchors/size,
    Rigidbody2D (!u!50), Rigidbody (!u!54), BoxCollider2D (!u!61),
    CircleCollider2D (!u!58), BoxCollider (!u!65), SphereCollider (!u!135),
    AudioSource (!u!82), Animation (!u!111), Animator (!u!95),
    PhysicsMaterial2D / PhysicMaterial, and AnimationClip / AnimatorController
    assets. Does not invent any of those — missing components stay missing.
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
            r"Rigidbody|BoxCollider2D|CircleCollider2D|BoxCollider|"
            r"SphereCollider|Animation|Animator|Canvas|AudioSource):",
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
            rec["sprite"] = {
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
            rec["canvas"] = {
                "enabled": int(en.group(1)) if en else 1,
                "render_mode": int(rm.group(1)) if rm else 0,
                "camera_file_id": int(cam.group(1)) if cam else 0,
                "plane_distance": float(pd.group(1)) if pd else 100.0,
                "sorting_layer_id": int(sid.group(1)) if sid else 0,
                "sorting_order": int(so.group(1)) if so else 0,
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
                    # 0 Simple, 1 Sliced, 2 Tiled, 3 Filled
                    "image_type": int(itype.group(1)) if itype else 0,
                    "pixels_per_unit_multiplier": (
                        float(ppum.group(1)) if ppum else 1.0),
                }
            elif _is_ui_button_mb(block, g):
                rec["ui_button"] = _parse_ui_button(block)
            elif _is_ui_tmp_mb(block, g):
                rec["ui_tmp"] = _parse_ui_tmp(block, asset_guids)
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
            rec["camera"] = {
                "orthographic": int(ortho.group(1)) if ortho else 1,
                "orthographic_size": (
                    float(osize.group(1)) if osize else 5.0),
                "bg_r": float(bg.group(1)) if bg else 0.05,
                "bg_g": float(bg.group(2)) if bg else 0.05,
                "bg_b": float(bg.group(3)) if bg else 0.08,
                # Unity defaults when YAML omits clip planes.
                "near_clip": float(near.group(1)) if near else 0.3,
                "far_clip": float(far.group(1)) if far else 1000.0,
            }
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
            rec["rigidbody2d"] = {
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
        if kind == "BoxCollider2D":
            en = re.search(r"(?m)^\s+m_Enabled:\s*(\d+)", block)
            trig = re.search(r"(?m)^\s+m_IsTrigger:\s*(\d+)", block)
            off = re.search(
                r"m_Offset:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}", block)
            sz = re.search(
                r"m_Size:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}", block)
            rec["collider2d"] = {
                "kind": "box",
                "enabled": int(en.group(1)) if en else 1,
                "is_trigger": int(trig.group(1)) if trig else 0,
                "offset_x": float(off.group(1)) if off else 0.0,
                "offset_y": float(off.group(2)) if off else 0.0,
                "size_x": float(sz.group(1)) if sz else 1.0,
                "size_y": float(sz.group(2)) if sz else 1.0,
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

    # Join MonoBehaviour + Transform + SpriteRenderer onto the GameObject.
    gos = [r for r in by_id.values() if r.get("kind") == "GameObject"]
    for go in gos:
        kids = []
        for mid in re.findall(r"fileID:\s*(\d+)", go["raw"]):
            if mid in by_id and by_id[mid] is not go:
                kids.append(by_id[mid])
        pos = (0.0, 0.0, 0.0)
        scale = (1.0, 1.0, 1.0)
        rot = (0.0, 0.0, 0.0, 1.0)
        local_pos = pos
        local_rot = rot
        local_scale = scale
        xf = None
        script = None
        fields = {}
        object_refs = {}
        vec2_fields = {}
        vec3_fields = {}
        sprite = None
        ui_image = None
        ui_button = None
        ui_tmp = None
        canvas = None
        cam = None
        rb2d = None
        rb3d = None
        col2d = None
        col3d = None
        anim = None
        animator = None
        audiosources = []
        rect = None
        for k in kids:
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
                fields.update(k.get("fields") or {})
                object_refs.update(k.get("object_refs") or {})
                vec2_fields.update(k.get("vec2_fields") or {})
                vec3_fields.update(k.get("vec3_fields") or {})
                g = k.get("guid")
                if g and g in guid_to_script:
                    script = guid_to_script[g]
                if k.get("ui_image"):
                    ui_image = dict(k["ui_image"])
                if k.get("ui_button"):
                    ui_button = dict(k["ui_button"])
                if k.get("ui_tmp"):
                    ui_tmp = dict(k["ui_tmp"])
            if k.get("kind") == "SpriteRenderer" and k.get("sprite"):
                sprite = dict(k["sprite"])
            if k.get("kind") == "Canvas" and k.get("canvas"):
                canvas = dict(k["canvas"])
            if k.get("kind") == "Camera" and k.get("camera"):
                cam = dict(k["camera"])
            if k.get("kind") == "Rigidbody2D" and k.get("rigidbody2d"):
                rb2d = dict(k["rigidbody2d"])
                rb2d["file_id"] = k.get("file_id")
            if k.get("kind") == "Rigidbody" and k.get("rigidbody"):
                rb3d = dict(k["rigidbody"])
                rb3d["file_id"] = k.get("file_id")
            if k.get("kind") in ("BoxCollider2D", "CircleCollider2D") and k.get(
                    "collider2d"):
                col2d = dict(k["collider2d"])
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
            if col2d.get("kind") == "box":
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
        else:
            sprite = None
        class_name = None
        if script:
            class_name = _class_name_from_cs(script)
        # Every authored GO with a Transform — Find children need not be packed.
        if xf is not None:
            hierarchy.append({
                "name": go.get("name") or "obj",
                "xf_id": xf_id,
                "father_id": father_id,
                "go_id": go.get("file_id"),
                "has_canvas": bool(canvas),
                "has_image": bool(ui_image),
                "has_button": bool(ui_button),
                "has_tmp": bool(ui_tmp),
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
        # Image without sprite / TMP without font: drop. EventSystem: drop.
        # Prefab stubs with unresolved MB guids: keep (has_mb).
        if ui_image and not has_ui_draw and script is None and sprite is None:
            if not rb2d and not rb3d and not col2d and not col3d and not player:
                if not canvas and not ui_button and not ui_tmp:
                    continue
        if ui_tmp and not has_ui_draw and script is None and sprite is None:
            if (not rb2d and not rb3d and not col2d and not col3d and not player
                    and not canvas and not ui_button and not ui_image):
                continue
        if (script is None and sprite is None and not has_ui_draw
                and not rb2d and not rb3d and cam is None and not col2d
                and not col3d and not player and not canvas
                and (not has_mb or ui_scaffold_mb)):
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
                "go_id": go.get("file_id"),
                "fields": {},
                "script": None,
                "class": "_Canvas",
                "sprite": None,
                "canvas": canvas,
                "rect": rect,
                "ui_image": None,
                "ui_button": None,
                "ui_tmp": None,
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
            "go_id": go.get("file_id"),
            "fields": fields,
            "object_refs": object_refs,
            "script": script,
            "class": class_name or go.get("name") or "Obj",
            "sprite": sprite,
            "canvas": canvas,
            "rect": rect,
            "ui_image": ui_image,
            "ui_button": ui_button,
            "ui_tmp": ui_tmp,
            "rigidbody2d": rb2d,
            "rigidbody": rb3d,
            "collider2d": col2d,
            "collider3d": col3d,
            "anim_player": player,
            "audiosources": audiosources,
        })
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
    return objects, lights, cameras, hierarchy


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

    def _comp_type_span(angle_open, angle_close):
        """Return (simple_name, type_idx) for text inside `<…>`."""
        inner = text[angle_open + 1:angle_close]
        stripped = inner.strip()
        lead = len(inner) - len(inner.lstrip())
        base = angle_open + 1 + lead
        if "." in stripped:
            mty = re.search(r"(\w+)\s*$", stripped)
            if mty:
                return mty.group(1), base + mty.start(1)
            return stripped.rsplit(".", 1)[-1], base
        mty = re.match(r"(\w+)", stripped)
        if mty:
            return mty.group(1), base + mty.start(1)
        return stripped, base

    for m in re.finditer(
            r"(?:UnityEngine\.)?GameObject\.Find\s*\(", scan):
        open_p = m.end() - 1
        close_p = cpprust._match_paren(scan, open_p)
        if close_p is None:
            continue
        find_args = text[open_p + 1:close_p]
        end = close_p + 1
        comp_ty = None
        type_idx = None
        field = None
        axis = None
        # .GetComponent < T > ( ... )
        gm = re.match(r"\s*\.\s*GetComponent\s*<", scan[end:])
        if gm:
            angle_open = end + gm.end() - 1
            angle_close = cpprust._match_angle(scan, angle_open)
            if angle_close is None:
                continue
            comp_ty, type_idx = _comp_type_span(angle_open, angle_close)
            after_angle = scan[angle_close + 1:]
            if after_angle.find("(") < 0:
                continue
            g_open = angle_close + 1 + after_angle.find("(")
            g_close = cpprust._match_paren(scan, g_open)
            if g_close is None:
                continue
            end = g_close + 1
            fm = re.match(
                r"\s*\.\s*([A-Za-z_]\w*)\b(?:\s*\.\s*([xyz]))?",
                scan[end:])
            if fm:
                field = fm.group(1)
                axis = fm.group(2)
                end = end + fm.end()
        out.append({
            "start": m.start(),
            "end": end,
            "find_args": find_args.strip(),
            "component": comp_ty,
            "type_idx": type_idx,
            "field": field,
            "axis": axis,
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
        comp_ty, type_idx = _comp_type_span(angle_open, angle_close)
        after_angle = scan[angle_close + 1:]
        if after_angle.find("(") < 0:
            continue
        g_open = angle_close + 1 + after_angle.find("(")
        g_close = cpprust._match_paren(scan, g_open)
        if g_close is None:
            continue
        end = g_close + 1
        field = None
        axis = None
        fm = re.match(
            r"\s*\.\s*([A-Za-z_]\w*)\b(?:\s*\.\s*([xyz]))?",
            scan[end:])
        if fm:
            field = fm.group(1)
            axis = fm.group(2)
            end = end + fm.end()
        out.append({
            "start": m.start(),
            "end": end,
            "find_args": None,  # this GameObject
            "component": comp_ty,
            "type_idx": type_idx,
            "field": field,
            "axis": axis,
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


def _build_go_ui_component_maps(plan):
    """Authored UI component presence: type → set of GO names."""
    maps = {t: set() for t in _UI_GETCOMPONENT_TYPES}
    def mark(n, *tys):
        if not n:
            return
        for t in tys:
            if t in maps:
                maps[t].add(n)

    for cl in (plan.get("classes") or {}).values():
        for o in cl.get("instances") or []:
            n = o.get("name") or "obj"
            mark(n, "RectTransform")
            if o.get("canvas"):
                mark(n, "Canvas")
            if o.get("ui_image"):
                mark(n, "Image", "RawImage", "Selectable")
            if o.get("ui_button"):
                mark(n, "Button", "Selectable")
            if o.get("ui_tmp"):
                mark(n, "TMP_Text", "TextMeshProUGUI", "TextMeshPro",
                     "Selectable")
    for h in plan.get("scene_hierarchy") or []:
        n = h.get("name") or "obj"
        mark(n, "RectTransform")
        if h.get("has_canvas"):
            mark(n, "Canvas")
        if h.get("has_image"):
            mark(n, "Image", "RawImage", "Selectable")
        if h.get("has_button"):
            mark(n, "Button", "Selectable")
        if h.get("has_tmp"):
            mark(n, "TMP_Text", "TextMeshProUGUI", "TextMeshPro")
    return {t: sorted(s) for t, s in maps.items() if s}


def _extend_go_tables_for_find(plan, names, comps):
    """Add authored hierarchy-only GOs so Transform.Find can see children."""
    names = list(names)
    comps = {k: dict(v) for k, v in comps.items()}
    seen = set(names)
    for h in plan.get("scene_hierarchy") or []:
        n = h.get("name") or "obj"
        if n in seen:
            continue
        seen.add(n)
        names.append(n)
        comps.setdefault(n, {})
    return names, comps


def _build_go_parents(plan):
    """go index → parent go index via authored m_Father (for activeInHierarchy)."""
    names = plan.get("go_names") or []
    if not names:
        return []
    name_i = {n: i for i, n in enumerate(names)}
    xf_to_go = {}
    # Packed MB instances first, then hierarchy-only GOs.
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            n = o.get("name") or "obj"
            xid = o.get("xf_id")
            if xid is not None and str(xid) != "0" and n in name_i:
                xf_to_go[str(xid)] = name_i[n]
    for h in plan.get("scene_hierarchy") or []:
        n = h.get("name") or "obj"
        xid = h.get("xf_id")
        if xid is not None and str(xid) != "0" and n in name_i:
            xf_to_go.setdefault(str(xid), name_i[n])
    parents = [-1] * len(names)
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            n = o.get("name") or "obj"
            if n not in name_i:
                continue
            gi = name_i[n]
            fid = o.get("father_id")
            if not fid or str(fid) == "0":
                continue
            parents[gi] = int(xf_to_go.get(str(fid), -1))
    for h in plan.get("scene_hierarchy") or []:
        n = h.get("name") or "obj"
        if n not in name_i:
            continue
        gi = name_i[n]
        if parents[gi] >= 0:
            continue
        fid = h.get("father_id")
        if not fid or str(fid) == "0":
            continue
        parents[gi] = int(xf_to_go.get(str(fid), -1))
    return parents


def _build_go_sibling_indices(go_parents):
    """Sibling index among children of the same parent (authored seed order).

    Unity GetSiblingIndex: position in the parent's child list. Seed by GO
    table order under each parent; SetParent appends as last sibling.
    """
    n = len(go_parents or [])
    sib = [0] * n
    by_parent = {}
    for i, p in enumerate(go_parents or []):
        by_parent.setdefault(int(p), []).append(i)
    for kids in by_parent.values():
        for idx, go in enumerate(kids):
            sib[go] = idx
    return sib


def _build_ui_buttons(plan):
    """Authored uGUI Buttons: normalized hit, ColorBlock, SetActive onClick."""
    names = plan.get("go_names") or []
    go_by_id = {}
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            gid = str(o.get("go_id") or "")
            n = o.get("name") or "obj"
            if gid and n in names:
                go_by_id[gid] = names.index(n)
    buttons = []
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            ub = o.get("ui_button")
            hit = o.get("ui_hit")
            if not ub or not hit:
                continue
            if not int(ub.get("interactable", 1)):
                continue
            n = o.get("name") or "obj"
            if n not in names:
                continue
            self_go = names.index(n)
            calls = []
            for c in ub.get("onclick") or []:
                if c.get("method") != "SetActive":
                    continue
                tgt = go_by_id.get(str(c.get("target_go") or ""))
                if tgt is None:
                    continue
                calls.append({
                    "target_go": int(tgt),
                    "bool_arg": int(c.get("bool_arg") or 0),
                })
            if not calls:
                continue
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


def _collect_addcomponent_types(analyses):
    types = set()
    for a in analyses:
        types |= set(a.get("addcomponent_types") or [])
    return types


def _addcomponent_budget(analyses, plan):
    """Extra slots per type: one per instance of each class that calls AddComponent<T>.

    A successful first add needs a pool slot. DisallowMultiple types that
    already have an authored component never allocate; budget still covers
    GOs that lack one.
    """
    budget = {}
    class_n = {n: int(cl.get("n") or 0) for n, cl in plan["classes"].items()}
    for a in analyses:
        for c in a.get("classes") or []:
            cname = c["name"]
            n = class_n.get(cname, 1) or 1
            bodies = "\n".join(m.get("body") or "" for m in c.get("methods") or [])
            for m in re.finditer(
                    r"AddComponent\s*<\s*(?:UnityEngine\.)?(\w+)\s*>", bodies):
                t = m.group(1)
                budget[t] = budget.get(t, 0) + n
    return budget


def _disallow_multiple_types(analyses):
    """Type names that refuse a second AddComponent (Unity attribute / builtins)."""
    out = set(_DISALLOW_MULTIPLE_BUILTINS)
    for a in analyses:
        for c in a.get("classes") or []:
            if c.get("disallow_multiple"):
                out.add(c["name"])
    return out


def _gos_with_sprite(plan):
    """Authored GameObject names that already have a SpriteRenderer."""
    names = set()
    for cl in (plan.get("classes") or {}).values():
        for o in cl.get("instances") or []:
            if o.get("sprite"):
                names.add(o.get("name") or "")
    return names


def _validate_addcomponent_types(types, plan, analyses=None):
    known = set(plan.get("classes") or {}) | _ADDABLE_BUILTINS
    analyses = analyses or []
    for t in sorted(types):
        if t in _REFUSED_ADDCOMPONENT or t not in known:
            _raise_unknown_component_type(
                t, analyses, ops=("AddComponent",))


def _validate_getcomponent_types(types, plan, analyses=None):
    """GetComponent<T> for unknown T → CS0246 (same as missing type).

    Authored uGUI types (Canvas, Image, RectTransform, …) are allowed —
    GetComponent looks them up; AddComponent<Canvas> invent stays refused.
    """
    known = (set(plan.get("classes") or {})
             | _ADDABLE_BUILTINS
             | _PHYSICS_COMPONENTS
             | _UI_GETCOMPONENT_TYPES)
    analyses = analyses or []
    for t in sorted(types):
        if t not in known:
            _raise_unknown_component_type(
                t, analyses, ops=("GetComponent", "AddComponent"))


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

    text = re.sub(
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

    text = re.sub(
        r"(?<![.\w])(\w+)\s*\.\s*(playOnAwake|loop|mute)\s*=\s*([^;]+);",
        repl_bool, text)

    def repl_float(m):
        recv, prop, rhs = m.group(1), m.group(2), m.group(3).strip()
        if recv not in as_recvs:
            return m.group(0)
        return "_AudioSource_%s[%s] = %s;" % (prop, recv, rhs)

    text = re.sub(
        r"(?<![.\w])(\w+)\s*\.\s*(volume|pitch)\s*=\s*([^;]+);",
        repl_float, text)

    def repl_clip(m):
        recv, rhs = m.group(1), m.group(2).strip()
        if recv not in as_recvs:
            return m.group(0)
        if rhs == "null":
            rhs = "-1"
        return "_AudioSource_clip[%s] = %s;" % (recv, rhs)

    text = re.sub(
        r"(?<![.\w])(\w+)\s*\.\s*clip\s*=\s*([^;]+);",
        repl_clip, text)

    def repl_call(m):
        recv, meth = m.group(1), m.group(2)
        if recv not in as_recvs:
            return m.group(0)
        return "AudioSource_%s(%s)" % (meth, recv)

    text = re.sub(
        r"(?<![.\w])(\w+)\s*\.\s*(Play|Stop)\s*\(\s*\)",
        repl_call, text)

    # Bool/float reads: recv.volume / recv.loop
    def repl_read_float(m):
        recv, prop = m.group(1), m.group(2)
        if recv not in as_recvs:
            return m.group(0)
        return "_AudioSource_%s[%s]" % (prop, recv)

    text = re.sub(
        r"(?<![.\w])(\w+)\s*\.\s*(volume|pitch)\b",
        repl_read_float, text)

    def repl_read_bool(m):
        recv, prop = m.group(1), m.group(2)
        if recv not in as_recvs:
            return m.group(0)
        field = bool_map[prop]
        return "_AudioSource_%s[%s]" % (field, recv)

    text = re.sub(
        r"(?<![.\w])(\w+)\s*\.\s*(playOnAwake|loop|mute)\b",
        repl_read_bool, text)

    def repl_read_clip(m):
        recv = m.group(1)
        if recv not in as_recvs:
            return m.group(0)
        return "_AudioSource_clip[%s]" % recv

    text = re.sub(
        r"(?<![.\w])(\w+)\s*\.\s*clip\b",
        repl_read_clip, text)
    return text


def _rewrite_addcomponent(text, plan, this_class):
    """Lower gameObject.AddComponent<T>() / AddComponent<T>() to C helpers.

    Also ``audioSource.gameObject.AddComponent<T>()`` (component → owner GO).
    Returns (text, locals_ty) where locals_ty maps local name → component type
    for Console/Debug ToString wrapping.
    """
    this_idn = _c_ident(this_class)
    go_expr = "_engine_go_of_%s(i)" % this_idn
    locals_ty = {}
    as_fields = set()
    cl = (plan.get("classes") or {}).get(this_class) or {}
    for f in cl.get("fields") or []:
        if f.get("ty") == "AudioSource":
            as_fields.add(f["name"])
    for name, _ty, _bits, kind in cl.get("members") or []:
        if str(kind) == "idx:AudioSource":
            as_fields.add(name)

    def _go_of_recv(recv):
        if not recv or recv in ("this", "gameObject"):
            return go_expr
        if recv in as_fields or locals_ty.get(recv) == "AudioSource":
            return "_AudioSource_owner_go[%s]" % recv
        return go_expr

    # AudioSource asrc = musicSource.gameObject.AddComponent<AudioSource>();
    def repl_typed_go(m):
        var, recv, comp = m.group(1), m.group(2), m.group(3)
        locals_ty[var] = comp
        return "int %s = GameObject_AddComponent_%s(%s)" % (
            var, _c_ident(comp), _go_of_recv(recv))

    text = re.sub(
        r"(?:(?:UnityEngine\.)?\w+)\s+(\w+)\s*=\s*"
        r"(\w+)\s*\.\s*gameObject\s*\.\s*AddComponent\s*<\s*"
        r"(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)",
        repl_typed_go, text)

    def repl_bare_go(m):
        recv, comp = m.group(1), m.group(2)
        return "GameObject_AddComponent_%s(%s)" % (
            _c_ident(comp), _go_of_recv(recv))

    text = re.sub(
        r"(\w+)\s*\.\s*gameObject\s*\.\s*AddComponent\s*<\s*"
        r"(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)",
        repl_bare_go, text)

    def repl_typed(m):
        var, comp = m.group(1), m.group(2)
        locals_ty[var] = comp
        return "int %s = GameObject_AddComponent_%s(%s)" % (
            var, _c_ident(comp), go_expr)

    # Camera cam = gameObject.AddComponent<Camera>();
    text = re.sub(
        r"(?:(?:UnityEngine\.)?\w+)\s+(\w+)\s*=\s*"
        r"(?:(?:this|gameObject)\s*\.\s*)?AddComponent\s*<\s*"
        r"(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)",
        repl_typed, text)

    def repl_bare(m):
        return "GameObject_AddComponent_%s(%s)" % (
            _c_ident(m.group(1)), go_expr)

    text = re.sub(
        r"(?:(?:this|gameObject)\s*\.\s*)?AddComponent\s*<\s*"
        r"(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)",
        repl_bare, text)
    return text, locals_ty


def _wrap_log_collision2d_tostring(text, param):
    """Console/Debug of a Collision2D param → Collision2D_ToString(handle)."""
    if not param:
        return text
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
        if args == param:
            args = "Collision2D_ToString(%s)" % param
        out.append("%s(%s)" % (callee, args))
        i = j + 1
    return "".join(out)


def _wrap_log_component_tostring(text, locals_ty):
    """Console/Debug of an AddComponent local → Type_ToString(index)."""
    if not locals_ty:
        return text
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
        if re.match(r"^[A-Za-z_]\w*$", args) and args in locals_ty:
            ty = locals_ty[args]
            args = "%s_ToString(%s)" % (_c_ident(ty), args)
        out.append("%s(%s)" % (callee, args))
        i = j + 1
    return "".join(out)


_PHYSICS_COMPONENTS = frozenset(("Rigidbody2D", "Rigidbody"))

# MonoBehaviour 2D collision messages (Unity Physics2D).
_COLLISION2D_MSGS = (
    "OnCollisionEnter2D",
    "OnCollisionStay2D",
    "OnCollisionExit2D",
)


def _collision2d_arg_name(args):
    """Param name from `OnCollisionEnter2D(Collision2D coll)`, or None."""
    if not args:
        return None
    m = re.match(
        r"(?:UnityEngine\.)?Collision2D\s+(\w+)\s*$",
        args.strip())
    return m.group(1) if m else None


def _build_rigidbody_tables(plan):
    """Authored Rigidbody2D / Rigidbody → packed tables linked to MB instances."""
    rb2d = []
    rb3d = []
    go_rb2d = {}  # go_name -> rb2d index
    go_rb3d = {}
    rb2d_by_file_id = {}
    rb3d_by_file_id = {}
    for cname, cl in sorted(plan["classes"].items()):
        for i, o in enumerate(cl.get("instances") or []):
            n = o.get("name") or "obj"
            r2 = o.get("rigidbody2d")
            if r2:
                go_rb2d[n] = len(rb2d)
                fid = r2.get("file_id")
                if fid is not None and str(fid) != "0":
                    rb2d_by_file_id[str(fid)] = len(rb2d)
                rb2d.append({
                    "name": n,
                    "owner_class": cname,
                    "owner_inst": i,
                    "file_id": fid,
                    "body_type": int(r2.get("body_type") or 0),
                    "mass": float(r2.get("mass") or 1.0),
                    "gravity_scale": float(r2.get("gravity_scale") or 1.0),
                    "linear_damping": float(r2.get("linear_damping") or 0.0),
                    "vel_x": float(r2.get("vel_x") or 0.0),
                    "vel_y": float(r2.get("vel_y") or 0.0),
                })
            r3 = o.get("rigidbody")
            if r3:
                go_rb3d[n] = len(rb3d)
                fid = r3.get("file_id")
                if fid is not None and str(fid) != "0":
                    rb3d_by_file_id[str(fid)] = len(rb3d)
                rb3d.append({
                    "name": n,
                    "owner_class": cname,
                    "owner_inst": i,
                    "file_id": fid,
                    "mass": float(r3.get("mass") or 1.0),
                    "use_gravity": int(r3.get("use_gravity")
                                       if r3.get("use_gravity") is not None
                                       else 1),
                    "drag": float(r3.get("drag") or 0.0),
                    "vel_x": float(r3.get("vel_x") or 0.0),
                    "vel_y": float(r3.get("vel_y") or 0.0),
                    "vel_z": float(r3.get("vel_z") or 0.0),
                })
    return (rb2d, rb3d, go_rb2d, go_rb3d, rb2d_by_file_id, rb3d_by_file_id)


def _build_audiosource_tables(plan):
    """Authored AudioSource (!u!82) → packed pool; clip guids → opaque indices."""
    sources = []
    go_first = {}  # go_name → first AudioSource index (GetComponent)
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
            for a in o.get("audiosources") or []:
                fid = a.get("file_id")
                idx = len(sources)
                if n not in go_first:
                    go_first[n] = idx
                if fid is not None and str(fid) != "0":
                    by_file_id[str(fid)] = idx
                sources.append({
                    "name": n,
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


def _attach_transform_parents(plan):
    """Wire authored m_Father → packed parent for live world composition.

    Unity stores localPosition; world = parent_world ∘ local. Pack keeps
    local in instance pos when a packed parent exists. Bodies with their own
    Rigidbody / Rigidbody2D stay independent (Unity simulates them separately).
    UI Images whose Canvas is not a packed body keep baked world `pos`.
    Main Camera under a packed body follows the same rule via Camera_main_pos_*.
    """
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    xf_to = {}
    for cname, cl in plan["classes"].items():
        for i, o in enumerate(cl.get("instances") or []):
            xid = o.get("xf_id")
            if xid is not None and str(xid) != "0":
                xf_to[str(xid)] = (cname, i)
    any_parent = False
    for cname, cl in plan["classes"].items():
        for i, o in enumerate(cl.get("instances") or []):
            o["xf_parent_class"] = None
            o["xf_parent_class_id"] = -1
            o["xf_parent_inst"] = 0
            if o.get("rigidbody2d") or o.get("rigidbody"):
                continue
            fid = o.get("father_id")
            if not fid or str(fid) == "0":
                continue
            hit = xf_to.get(str(fid))
            if not hit:
                continue
            pc, pi = hit
            if pc == cname and pi == i:
                continue
            o["xf_parent_class"] = pc
            o["xf_parent_class_id"] = int(class_ids[pc])
            o["xf_parent_inst"] = int(pi)
            any_parent = True
    cam = plan.get("camera")
    if cam is not None:
        cam["xf_parent_class"] = None
        cam["xf_parent_class_id"] = -1
        cam["xf_parent_inst"] = 0
        fid = cam.get("father_id")
        if fid and str(fid) != "0":
            hit = xf_to.get(str(fid))
            if hit:
                pc, pi = hit
                cam["xf_parent_class"] = pc
                cam["xf_parent_class_id"] = int(class_ids[pc])
                cam["xf_parent_inst"] = int(pi)
                any_parent = True
    plan["has_transform_parents"] = any_parent
    plan["camera_follows_parent"] = bool(
        cam and cam.get("xf_parent_class"))


def _instance_storage_pos(o):
    """Coords stored in instance arrays: local under a live parent, else world."""
    if o.get("xf_parent_class"):
        lp = o.get("local_pos") or (0.0, 0.0, 0.0)
        return (float(lp[0]), float(lp[1]),
                float(lp[2]) if len(lp) > 2 else 0.0)
    p = o.get("pos") or (0.0, 0.0, 0.0)
    return (float(p[0]), float(p[1]),
            float(p[2]) if len(p) > 2 else 0.0)


def _build_collider2d_tables(plan):
    """Authored BoxCollider2D / CircleCollider2D → packed contact table."""
    cols = []
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    # Map (class, inst) → rb2d index for dynamic flag.
    rb_of = {}
    for ri, r in enumerate(plan.get("rigidbody2d") or []):
        rb_of[(r["owner_class"], r["owner_inst"])] = ri
    for cname, cl in sorted(plan["classes"].items()):
        cid = class_ids[cname]
        for i, o in enumerate(cl.get("instances") or []):
            c = o.get("collider2d")
            if not c or not c.get("enabled", 1):
                continue
            rb_i = rb_of.get((cname, i))
            body = 2  # static (no RB)
            if rb_i is not None:
                body = int((plan["rigidbody2d"][rb_i]).get("body_type") or 0)
            kind = 0 if c.get("kind") == "box" else 1
            cols.append({
                "name": o.get("name") or "obj",
                "owner_class": cname,
                "owner_class_id": cid,
                "owner_inst": i,
                "rb2d": rb_i if rb_i is not None else -1,
                "body_type": body,  # 0 dynamic, 1 kinematic, 2 static
                "kind": kind,
                "is_trigger": int(c.get("is_trigger") or 0),
                "ox": float(c.get("ox") or 0.0),
                "oy": float(c.get("oy") or 0.0),
                "hw": float(c.get("hw") or 0.5),
                "hh": float(c.get("hh") or 0.5),
                "cos_z": float(c.get("cos_z") or 1.0),
                "sin_z": float(c.get("sin_z") or 0.0),
                "friction": float(c.get("friction")
                                  if c.get("friction") is not None
                                  else _DEFAULT_MAT2D["friction"]),
                "bounciness": float(c.get("bounciness")
                                    if c.get("bounciness") is not None
                                    else 0.0),
                "friction_combine": int(c.get("friction_combine") or 0),
                "bounce_combine": int(c.get("bounce_combine") or 0),
            })
    return cols


def _build_collider3d_tables(plan):
    """Authored BoxCollider / SphereCollider → packed contact table."""
    cols = []
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    rb_of = {}
    for ri, r in enumerate(plan.get("rigidbody") or []):
        rb_of[(r["owner_class"], r["owner_inst"])] = ri
    for cname, cl in sorted(plan["classes"].items()):
        cid = class_ids[cname]
        for i, o in enumerate(cl.get("instances") or []):
            c = o.get("collider3d")
            if not c or not c.get("enabled", 1):
                continue
            rb_i = rb_of.get((cname, i))
            body = 0 if rb_i is not None else 2  # dynamic if RB else static
            kind = 0 if c.get("kind") == "box" else 1
            cols.append({
                "name": o.get("name") or "obj",
                "owner_class": cname,
                "owner_class_id": cid,
                "owner_inst": i,
                "rb3d": rb_i if rb_i is not None else -1,
                "body_type": body,
                "kind": kind,
                "is_trigger": int(c.get("is_trigger") or 0),
                "ox": float(c.get("ox") or 0.0),
                "oy": float(c.get("oy") or 0.0),
                "oz": float(c.get("oz") or 0.0),
                "hw": float(c.get("hw") or 0.5),
                "hh": float(c.get("hh") or 0.5),
                "hd": float(c.get("hd") or 0.5),
                "dynamic_friction": float(
                    c.get("dynamic_friction")
                    if c.get("dynamic_friction") is not None
                    else _DEFAULT_MAT3D["dynamic_friction"]),
                "static_friction": float(
                    c.get("static_friction")
                    if c.get("static_friction") is not None
                    else _DEFAULT_MAT3D["static_friction"]),
                "bounciness": float(c.get("bounciness")
                                    if c.get("bounciness") is not None
                                    else 0.0),
                "friction_combine": int(c.get("friction_combine") or 0),
                "bounce_combine": int(c.get("bounce_combine") or 0),
            })
    return cols


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
                "speed": float(p.get("speed") or 1.0),
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
        "players": players,
        "sprite_keys": sprite_keys,
        "sprite_binds": sprite_binds,
    }


def _resolve_transform_field_targets(plan):
    """Authored Transform field refs (fileID) → (class_id, inst) per owner.

    Player.graphicsTrs → Graphics Transform fileID → Graphics instance.
    """
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    xf_to = {}
    for cname, cl in plan["classes"].items():
        for i, o in enumerate(cl.get("instances") or []):
            xid = o.get("xf_id")
            if xid is not None and str(xid) != "0":
                xf_to[str(xid)] = (cname, i)
    targets = {}
    scale_classes = set()
    for cname, cl in plan["classes"].items():
        for f in cl.get("fields") or []:
            if f.get("ty") != "Transform":
                continue
            fname = f["name"]
            row = []
            for o in cl.get("instances") or []:
                refs = o.get("object_refs") or {}
                fid = refs.get(fname)
                hit = xf_to.get(str(fid)) if fid else None
                if hit:
                    tc, ti = hit
                    row.append((int(class_ids[tc]), int(ti), tc))
                    scale_classes.add(tc)
                else:
                    row.append(None)
            targets[(cname, fname)] = row
    plan["transform_field_targets"] = targets
    live = set(plan.get("live_scale_classes") or [])
    live |= scale_classes
    plan["live_scale_classes"] = sorted(live)
    mutable = set(plan.get("sprite_draw_mutable") or [])
    mutable |= scale_classes
    plan["sprite_draw_mutable"] = sorted(mutable)


def _match_call_args(text, open_paren):
    """Index of '(' → (args_str, index_after_closing_paren) or None."""
    if open_paren >= len(text) or text[open_paren] != "(":
        return None
    depth = 1
    j = open_paren + 1
    while j < len(text) and depth:
        c = text[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:j], j + 1
        j += 1
    return None


def _rewrite_extensions_set_world_scale(text, cl, plan):
    """Extensions.SetWorldScale + Vector2.SetX/SetZ → _engine_set_world_scale.

    Authored:
      graphicsTrs.SetWorldScale(multSize.SetX(multSize.x * xSize).SetZ(1));
    """
    idn = _c_ident(cl["name"])
    transform_fields = [
        f["name"] for f in (cl.get("fields") or [])
        if f.get("ty") == "Transform"]
    vec2_fields = set(cl.get("vec2_fields") or [])
    if not transform_fields:
        return text

    out = []
    i = 0
    while i < len(text):
        found = None
        for fname in transform_fields:
            pat = (r"(?<![_\w])%s\s*\.\s*SetWorldScale\s*\("
                   % re.escape(fname))
            m = re.search(pat, text[i:])
            if not m:
                continue
            if found is None or m.start() < found[0]:
                found = (m.start(), m.end(), fname)
        if not found:
            out.append(text[i:])
            break
        start_rel, end_rel, fname = found
        abs_start = i + start_rel
        abs_open = i + end_rel - 1
        out.append(text[i:abs_start])
        matched = _match_call_args(text, abs_open)
        if not matched:
            out.append(text[abs_start:abs_open + 1])
            i = abs_open + 1
            continue
        arg, after = matched
        arg_s = arg.strip()
        sx = sy = sz = None
        sx_m = re.match(r"(?s)^(\w+)\s*\.\s*SetX\s*\(", arg_s)
        if sx_m and sx_m.group(1) in vec2_fields:
            vname = sx_m.group(1)
            open_x = sx_m.end() - 1
            ax = _match_call_args(arg_s, open_x)
            if ax:
                sx_arg, after_x = ax
                rest = arg_s[after_x:]
                zm = re.match(r"\s*\.\s*SetZ\s*\(", rest)
                if zm:
                    open_z = after_x + zm.end() - 1
                    az = _match_call_args(arg_s, open_z)
                    if az:
                        sx = sx_arg.strip()
                        sy = "%s_get_%s_y(i)" % (idn, vname)
                        sz = az[0].strip()
        if sx is None:
            nm = re.match(r"(?s)^new\s+Vector3\s*\((.*)\)\s*$", arg_s)
            if nm:
                parts = _split_call_args(nm.group(1))
                if len(parts) >= 3:
                    sx, sy, sz = parts[0], parts[1], parts[2]
        if sx is None:
            out.append(text[abs_start:after])
            i = after
            continue
        for vf in vec2_fields:
            sx = re.sub(r"(?<![_\w])%s\.x\b" % vf,
                        "%s_get_%s_x(i)" % (idn, vf), sx)
            sx = re.sub(r"(?<![_\w])%s\.y\b" % vf,
                        "%s_get_%s_y(i)" % (idn, vf), sx)
            sz = re.sub(r"(?<![_\w])%s\.x\b" % vf,
                        "%s_get_%s_x(i)" % (idn, vf), sz)
            sz = re.sub(r"(?<![_\w])%s\.y\b" % vf,
                        "%s_get_%s_y(i)" % (idn, vf), sz)
        out.append(
            "_engine_set_world_scale("
            "_%s_%s_target_class[i], (unsigned)_%s_%s_target_inst[i], "
            "(%s), (%s), (%s))" % (
                idn, fname, idn, fname, sx, sy, sz))
        i = after
    return "".join(out)


def _rewrite_rigidbody_assigns(text, plan, this_class):
    """Lower Rigidbody(2D).linearVelocity / .velocity assigns.

    Supports:
      GetComponent<Rigidbody2D>().linearVelocity = new Vector2(x, y);
      rb.linearVelocity = rb.linearVelocity.SetX(expr);
      rb.linearVelocity = new Vector2(x, y);
    and Rigidbody / SetY / SetZ / velocity aliases.
    """
    this_idn = _c_ident(this_class)
    go_this = "_engine_go_of_%s(i)" % this_idn
    cl = (plan.get("classes") or {}).get(this_class) or {}
    rb2d_fields = [
        f["name"] for f in (cl.get("fields") or [])
        if f.get("ty") == "Rigidbody2D"]
    rb3d_fields = [
        f["name"] for f in (cl.get("fields") or [])
        if f.get("ty") == "Rigidbody"]

    def repl_2d_new(m):
        args = _split_call_args(m.group(1))
        if len(args) < 2:
            return m.group(0)
        return (
            "{ int _up_rb = GameObject_GetComponent_Rigidbody2D(%s); "
            "if (_up_rb >= 0) { _Rigidbody2D_vel_x[_up_rb] = (%s); "
            "_Rigidbody2D_vel_y[_up_rb] = (%s); } }"
            % (go_this, args[0], args[1])
        )

    def repl_3d_new(m):
        args = _split_call_args(m.group(1))
        if len(args) < 3:
            return m.group(0)
        return (
            "{ int _up_rb = GameObject_GetComponent_Rigidbody(%s); "
            "if (_up_rb >= 0) { _Rigidbody_vel_x[_up_rb] = (%s); "
            "_Rigidbody_vel_y[_up_rb] = (%s); "
            "_Rigidbody_vel_z[_up_rb] = (%s); } }"
            % (go_this, args[0], args[1], args[2])
        )

    text = re.sub(
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody2D\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
        r"new\s+Vector2\s*\((.*?)\)\s*;",
        repl_2d_new, text, flags=re.S)
    text = re.sub(
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
        r"new\s+Vector3\s*\((.*?)\)\s*;",
        repl_3d_new, text, flags=re.S)

    def repl_2d_setx(m):
        return (
            "{ int _up_rb = GameObject_GetComponent_Rigidbody2D(%s); "
            "if (_up_rb >= 0) { _Rigidbody2D_vel_x[_up_rb] = (%s); } }"
            % (go_this, m.group(1).strip())
        )

    def repl_2d_sety(m):
        return (
            "{ int _up_rb = GameObject_GetComponent_Rigidbody2D(%s); "
            "if (_up_rb >= 0) { _Rigidbody2D_vel_y[_up_rb] = (%s); } }"
            % (go_this, m.group(1).strip())
        )

    text = re.sub(
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody2D\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody2D\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*\.\s*SetX\s*\((.*?)\)\s*;",
        repl_2d_setx, text, flags=re.S)
    text = re.sub(
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody2D\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody2D\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*\.\s*SetY\s*\((.*?)\)\s*;",
        repl_2d_sety, text, flags=re.S)

    def repl_3d_set(axis):
        def _repl(m):
            return (
                "{ int _up_rb = GameObject_GetComponent_Rigidbody(%s); "
                "if (_up_rb >= 0) { _Rigidbody_vel_%s[_up_rb] = (%s); } }"
                % (go_this, axis, m.group(1).strip())
            )
        return _repl

    for axis in ("X", "Y", "Z"):
        text = re.sub(
            r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody\s*>"
            r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
            r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody\s*>"
            r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*\.\s*Set%s\s*\((.*?)\)\s*;"
            % axis,
            repl_3d_set(axis.lower()), text, flags=re.S)

    # Field-based: rb.linearVelocity = rb.linearVelocity.SetX(expr);
    for fname in rb2d_fields:
        get_rb = "(int)%s_get_%s(i)" % (this_idn, fname)

        def _set_xy(x_expr, y_expr, gr=get_rb):
            return (
                "{ int _up_rb = %s; if (_up_rb >= 0) { "
                "_Rigidbody2D_vel_x[_up_rb] = (%s); "
                "_Rigidbody2D_vel_y[_up_rb] = (%s); } }"
                % (gr, x_expr, y_expr)
            )

        def repl_setx(m, fn=fname, gr=get_rb):
            # Keep y; set x from SetX arg.
            return (
                "{ int _up_rb = %s; if (_up_rb >= 0) { "
                "_Rigidbody2D_vel_x[_up_rb] = (%s); } }"
                % (gr, m.group(1).strip())
            )

        def repl_sety(m, fn=fname, gr=get_rb):
            return (
                "{ int _up_rb = %s; if (_up_rb >= 0) { "
                "_Rigidbody2D_vel_y[_up_rb] = (%s); } }"
                % (gr, m.group(1).strip())
            )

        def repl_new2(m, gr=get_rb):
            args = _split_call_args(m.group(1))
            if len(args) < 2:
                return m.group(0)
            return _set_xy(args[0], args[1], gr)

        text = re.sub(
            r"(?<![_\w])%s\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
            r"(?:this\s*\.\s*)?%s\s*\.\s*(?:linearVelocity|velocity)\s*"
            r"\.\s*SetX\s*\((.*?)\)\s*;"
            % (re.escape(fname), re.escape(fname)),
            repl_setx, text, flags=re.S)
        text = re.sub(
            r"(?<![_\w])%s\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
            r"(?:this\s*\.\s*)?%s\s*\.\s*(?:linearVelocity|velocity)\s*"
            r"\.\s*SetY\s*\((.*?)\)\s*;"
            % (re.escape(fname), re.escape(fname)),
            repl_sety, text, flags=re.S)
        text = re.sub(
            r"(?<![_\w])%s\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
            r"new\s+Vector2\s*\((.*?)\)\s*;" % re.escape(fname),
            repl_new2, text, flags=re.S)

    for fname in rb3d_fields:
        get_rb = "(int)%s_get_%s(i)" % (this_idn, fname)

        def _axis_set(axis, m, gr=get_rb):
            return (
                "{ int _up_rb = %s; if (_up_rb >= 0) { "
                "_Rigidbody_vel_%s[_up_rb] = (%s); } }"
                % (gr, axis, m.group(1).strip())
            )

        def repl_new3(m, gr=get_rb):
            args = _split_call_args(m.group(1))
            if len(args) < 3:
                return m.group(0)
            return (
                "{ int _up_rb = %s; if (_up_rb >= 0) { "
                "_Rigidbody_vel_x[_up_rb] = (%s); "
                "_Rigidbody_vel_y[_up_rb] = (%s); "
                "_Rigidbody_vel_z[_up_rb] = (%s); } }"
                % (gr, args[0], args[1], args[2])
            )

        for axis in ("X", "Y", "Z"):
            text = re.sub(
                r"(?<![_\w])%s\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
                r"(?:this\s*\.\s*)?%s\s*\.\s*(?:linearVelocity|velocity)\s*"
                r"\.\s*Set%s\s*\((.*?)\)\s*;"
                % (re.escape(fname), re.escape(fname), axis),
                lambda m, ax=axis.lower(): _axis_set(ax, m),
                text, flags=re.S)
        text = re.sub(
            r"(?<![_\w])%s\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
            r"new\s+Vector3\s*\((.*?)\)\s*;" % re.escape(fname),
            repl_new3, text, flags=re.S)

    return text


def _nre_at_expr(site, line):
    """C expr that raises Unity-style NullReferenceException at *line*."""
    return (
        "_engine_null_reference_at(%s, %s, %s, %d)"
        % (_c_string(site["class"]), _c_string(site["method"]),
           _c_string(site["path"]), int(line))
    )


def _rewrite_find_getcomponent(text, plan, this_class, site=None):
    """Lower Find/GetComponent chains using the authored GO tables.

    `GameObject.Find` name lookup is always runtime (`strcmp` on the packed
    name table) — missing names yield -1 like Unity null, not a PackError.
    Calling GetComponent / reading a field on that null is a
    NullReferenceException (`_engine_null_reference_at`), matching Unity.
    Authored Rigidbody / Rigidbody2D are first-class GetComponent targets.
    """
    chains = _ast_find_getcomponent_chains(text)
    if not chains:
        return text

    if site is None:
        site = {
            "class": this_class,
            "method": "?",
            "path": "?",
            "body_abs": 0,
            "file_text": text,
        }

    def _line_at(offset_in_body):
        ft = site.get("file_text") or ""
        abs_i = int(site.get("body_abs") or 0) + int(offset_in_body)
        if not ft:
            return 0
        return ft.count("\n", 0, abs_i) + 1

    def _zero_for_field(comp, field):
        cl = (plan.get("classes") or {}).get(comp) or {}
        for name, ty, _bits, kind in cl.get("members") or []:
            if name != field:
                continue
            if kind in ("f16", "f32") or ty == "float":
                return "0.f"
            return "0"
        return "0.f"

    def _rb_field_expr(comp, field, axis, go_expr, line, body_idx=0):
        """GetComponent<Rigidbody2D/Rigidbody>().velocity.x / gravityScale."""
        get = "GameObject_GetComponent_%s(%s)" % (_c_ident(comp), go_expr)
        nre = _nre_at_expr(site, line)
        fl = (field or "").lower()
        if comp == "Rigidbody2D":
            if fl in ("gravityscale",):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 "
                        "? (%s, 0.f) "
                        ": _Rigidbody2D_gravity_scale[_up_rb]; })"
                        % (get, nre))
            if fl in ("lineardamping", "drag"):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 "
                        "? (%s, 0.f) "
                        ": _Rigidbody2D_linear_damping[_up_rb]; })"
                        % (get, nre))
            if fl in ("velocity", "linearvelocity") and axis in ("x", "y"):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 "
                        "? (%s, 0.f) "
                        ": _Rigidbody2D_vel_%s[_up_rb]; })"
                        % (get, nre, axis))
            if fl in ("mass",):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 "
                        "? (%s, 0.f) "
                        ": _Rigidbody2D_mass[_up_rb]; })"
                        % (get, nre))
        if comp == "Rigidbody":
            if fl in ("drag", "lineardamping"):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 "
                        "? (%s, 0.f) "
                        ": _Rigidbody_drag[_up_rb]; })"
                        % (get, nre))
            if fl in ("velocity", "linearvelocity") and axis in ("x", "y", "z"):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 "
                        "? (%s, 0.f) "
                        ": _Rigidbody_vel_%s[_up_rb]; })"
                        % (get, nre, axis))
            if fl in ("mass",):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 "
                        "? (%s, 0.f) "
                        ": _Rigidbody_mass[_up_rb]; })"
                        % (get, nre))
            if fl in ("usegravity",):
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 "
                        "? (%s, 0) "
                        ": _Rigidbody_use_gravity[_up_rb]; })"
                        % (get, nre))
        _raise_cs_at_site(
            site, body_idx, "CS1061",
            "'%s' does not contain a definition for '%s' and no "
            "accessible extension method '%s' accepting a first argument of "
            "type '%s' could be found (are you missing a using "
            "directive or an assembly reference?)"
            % (comp, field or "?", field or "?", comp))

    def _field_after_get(comp, field, go_expr, line, axis=None, body_idx=0):
        if comp in _PHYSICS_COMPONENTS:
            return _rb_field_expr(
                comp, field, axis, go_expr, line, body_idx)
        idn = _c_ident(comp)
        zero = _zero_for_field(comp, field)
        nre = _nre_at_expr(site, line)
        return (
            "({ int _up_gc = GameObject_GetComponent_%s(%s); "
            "_up_gc < 0 ? (%s, %s) "
            ": %s_get_%s((unsigned)_up_gc); })"
            % (idn, go_expr, nre, zero, idn, field)
        )

    def _known_component(comp):
        return (comp in (plan.get("classes") or {})
                or comp in _PHYSICS_COMPONENTS
                or comp in _ADDABLE_BUILTINS
                or comp in _UI_GETCOMPONENT_TYPES
                or comp in set(plan.get("addcomponent_types") or []))

    def _raise_unknown_comp(ch, comp):
        idx = ch.get("type_idx")
        if idx is None:
            idx = ch.get("start") or 0
        _raise_cs_at_site(site, idx, "CS0246", _CS0246 % comp)

    for ch in chains:
        comp = ch.get("component")
        field = ch.get("field")
        axis = ch.get("axis")
        line = _line_at(ch["start"])
        nre = _nre_at_expr(site, line)
        body_idx = ch.get("type_idx")
        if body_idx is None:
            body_idx = ch.get("start") or 0
        if ch.get("on_this"):
            if not comp:
                _raise_cs_at_site(
                    site, ch.get("start") or 0, "CS0305",
                    "Using the generic method 'GameObject.GetComponent<T>()' "
                    "requires 1 type arguments")
            this_idn = _c_ident(this_class)
            if not _known_component(comp):
                _raise_unknown_comp(ch, comp)
            go_expr = "_engine_go_of_%s(i)" % this_idn
            if field:
                repl = _field_after_get(
                    comp, field, go_expr, line, axis, body_idx)
            else:
                repl = "GameObject_GetComponent_%s(%s)" % (
                    _c_ident(comp), go_expr)
            text = text[:ch["start"]] + repl + text[ch["end"]:]
            continue

        find_args = ch.get("find_args") or ""
        go_expr = "GameObject_Find(%s)" % find_args
        if not comp:
            repl = go_expr
        else:
            if not _known_component(comp):
                _raise_unknown_comp(ch, comp)
            if field:
                # null Find or missing component → NRE at this source line.
                repl = _field_after_get(
                    comp, field, go_expr, line, axis, body_idx)
            else:
                # null.GetComponent<T>() throws; missing component returns null.
                repl = (
                    "({ int _up_go = %s; "
                    "_up_go < 0 ? (%s, -1) "
                    ": GameObject_GetComponent_%s(_up_go); })"
                    % (go_expr, nre, _c_ident(comp)))
        text = text[:ch["start"]] + repl + text[ch["end"]:]
    return text


def analyze_script(path, text=None):
    """Fields, methods, Unity API used, whether the script spawns."""
    if text is None:
        text = _read(path)
    # Player pack: editor-only regions are not code.
    text = _blank_unity_editor_regions(text)
    _check_csharp_lex(path, text)
    scan = cs2cpp._blank(text)
    apis = set()
    addcomponent_types = set()
    for m in _UNITY_API.finditer(scan):
        token = m.group(0)
        if token.startswith("AddComponent"):
            tm = re.search(r"AddComponent\s*<\s*(?:UnityEngine\.)?(\w+)\s*>",
                           token)
            if tm:
                tname = tm.group(1)
                addcomponent_types.add(tname)
                apis.add("AddComponent<%s>" % tname)
        elif token.startswith("GetComponent"):
            apis.add("GetComponent")
        elif "GameObject.Find" in token or token == "GameObject.Find":
            apis.add("GameObject.Find")
        else:
            apis.add(token)
    # Catch AddComponent even if _UNITY_API missed a variant.
    for m in re.finditer(
            r"AddComponent\s*<\s*(?:UnityEngine\.)?(\w+)\s*>", scan):
        addcomponent_types.add(m.group(1))
        apis.add("AddComponent<%s>" % m.group(1))
    # AST pass: precise Find / GetComponent detection (cpprust paren/angle).
    getcomponent_types = set()
    for ch in _ast_find_getcomponent_chains(text):
        if ch.get("find_args") is not None and not ch.get("on_this"):
            apis.add("GameObject.Find")
        if ch.get("component"):
            apis.add("GetComponent")
            getcomponent_types.add(ch["component"])
            if ch["component"] in _PHYSICS_COMPONENTS:
                apis.add(ch["component"])
        elif ch.get("on_this"):
            apis.add("GetComponent")
    if "transform.position" in scan:
        apis.add("transform.position")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*localPosition\b",
                 scan):
        apis.add("transform.localPosition")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*localRotation\b",
                 scan):
        apis.add("transform.localRotation")
    if re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*TransformPoint\s*\(",
            scan):
        apis.add("transform.TransformPoint")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*Rotate\s*\(",
                 scan):
        apis.add("transform.Rotate")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*LookAt\s*\(",
                 scan):
        apis.add("transform.LookAt")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*eulerAngles\b",
                 scan):
        apis.add("transform.eulerAngles")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*rotation\b",
                 scan):
        apis.add("transform.rotation")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*Find\s*\(",
                 scan):
        apis.add("transform.Find")
    # foo.transform.Find / trs.Find (Transform receiver — live GO index).
    if re.search(r"\.\s*transform\s*\.\s*Find\s*\(", scan):
        apis.add("transform.Find")
    if re.search(r"(?<![.\w])\w+\s*\.\s*Find\s*\(", scan):
        # May be List.Find etc.; rewrite only when receiver is Transform/GO.
        # Still record so parent tables emit when a Transform.Find exists.
        if re.search(
                r"(?:Transform|GameObject)\s+\w+\s*=|"
                r"\.\s*transform\s*\.\s*Find\s*\(|"
                r"(?:this\s*\.\s*)?transform\s*\.\s*Find\s*\(",
                scan):
            apis.add("transform.Find")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*localScale\b",
                 scan):
        apis.add("transform.localScale")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*parent\b",
                 scan):
        apis.add("transform.parent")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*gameObject\b",
                 scan):
        apis.add("transform.gameObject")
    if re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*SetParent\s*\(",
            scan):
        apis.add("transform.SetParent")
    # foo.transform.SetParent / trs.SetParent (Transform receiver).
    if re.search(r"\.\s*transform\s*\.\s*SetParent\s*\(", scan):
        apis.add("transform.SetParent")
    if re.search(r"(?<![.\w])\w+\s*\.\s*SetParent\s*\(", scan):
        apis.add("transform.SetParent")
    if re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*GetSiblingIndex\s*\(",
            scan):
        apis.add("transform.GetSiblingIndex")
    if re.search(r"\.\s*transform\s*\.\s*GetSiblingIndex\s*\(", scan):
        apis.add("transform.GetSiblingIndex")
    if re.search(r"(?<![.\w])\w+\s*\.\s*GetSiblingIndex\s*\(", scan):
        apis.add("transform.GetSiblingIndex")
    if re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
            r"worldToLocalMatrix\b",
            scan):
        apis.add("transform.worldToLocalMatrix")
    if re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
            r"localToWorldMatrix\b",
            scan):
        apis.add("transform.localToWorldMatrix")
    # Canvas invent only — using UnityEngine.UI / Image fields are authored OK.
    if (re.search(r"AddComponent\s*<\s*(?:UnityEngine\.)?Canvas\s*>", scan)
            or re.search(r"typeof\s*\(\s*Canvas\s*\)", scan)
            or re.search(r"Canvas\.ForceUpdateCanvases\b", scan)):
        apis.add("Canvas")
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
    if re.search(r"(?<![\w])(?:UnityEngine\.)?Application\.dataPath\b", scan):
        apis.add("Application.dataPath")
    if re.search(
            r"(?<![\w])(?:UnityEngine\.)?Application\.persistentDataPath\b",
            scan):
        apis.add("Application.persistentDataPath")
    if re.search(r"(?<![\w])(?:UnityEngine\.)?Application\.isEditor\b", scan):
        apis.add("Application.isEditor")
    if re.search(r"(?<![\w])(?:UnityEngine\.)?Application\.isPlaying\b", scan):
        apis.add("Application.isPlaying")
    if re.search(
            r"(?<![\w])(?:UnityEngine\.)?Application\.OpenURL\s*\(", scan):
        apis.add("Application.OpenURL")
    if re.search(
            r"(?<![\w])(?:UnityEngine\.)?Application\.productName\b", scan):
        apis.add("Application.productName")
    if re.search(
            r"(?<![\w])(?:UnityEngine\.)?Application\.Quit\s*\(", scan):
        apis.add("Application.Quit")
    if re.search(r"(?:System\.IO\.)?File\.WriteAllText\s*\(", scan):
        apis.add("File.WriteAllText")
    if re.search(r"(?:System\.IO\.)?File\.AppendAllText\s*\(", scan):
        apis.add("File.AppendAllText")
    if re.search(r"(?:System\.IO\.)?File\.WriteAllBytes\s*\(", scan):
        apis.add("File.WriteAllBytes")
    if re.search(r"(?:System\.IO\.)?File\.ReadAllBytes\s*\(", scan):
        apis.add("File.ReadAllBytes")
    if re.search(r"(?:System\.IO\.)?File\.Exists\s*\(", scan):
        apis.add("File.Exists")
    if re.search(r"(?:System\.IO\.)?File\.Delete\s*\(", scan):
        apis.add("File.Delete")
    if re.search(r"(?:System\.IO\.)?File\.CreateText\s*\(", scan):
        apis.add("File.CreateText")
    if re.search(r"(?:System\.IO\.)?File\.OpenText\s*\(", scan):
        apis.add("File.OpenText")
    if re.search(r"(?:System\.IO\.)?File\.Copy\s*\(", scan):
        apis.add("File.Copy")
    # C# string + value must not become C pointer arithmetic.
    if re.search(
            r'"\s*\+|'
            r"(?<![\w])Application\.(?:dataPath|persistentDataPath|"
            r"productName)\s*\+",
            scan):
        apis.add("string.+")
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
    if _DESTROY.search(scan):
        apis.add("Object.Destroy")
    uses_z = bool(re.search(r"(?<![\w.])Vector3\b", scan)
                  or re.search(r"(?<![\w.])Quaternion\b", scan)
                  or re.search(r"transform\.position\.z", scan))
    writes_pos = bool(re.search(
        r"transform\.position\s*=|"
        r"transform\.position\s*\+=|"
        r"transform\.localPosition\s*=|"
        r"transform\.localPosition\s*\+=|"
        r"transform\.Translate|"
        r"(?:^|[^\w.])(?:\w+\s*\.\s*)?transform\s*\.\s*SetParent\s*\(|"
        r"(?<![.\w])\w+\s*\.\s*SetParent\s*\(",
        scan))
    writes_rot = bool(re.search(
        r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
        r"(?:Rotate|LookAt)\s*\(", scan) or re.search(
        r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
        r"(?:eulerAngles|rotation|localRotation)\b",
        scan))

    types = cs2cpp._find_types(scan)
    classes = []
    for kind, name, start, brace, close in types:
        if kind not in ("class", "struct"):
            continue
        body = text[brace + 1:close]
        bscan = scan[brace + 1:close]
        fields = _fields_in(body, bscan, body_abs=brace + 1)
        methods = _methods_in(body, bscan, body_abs=brace + 1)
        refs = []
        for f in fields:
            if f["ty"] not in _PRIM and f["ty"] not in (
                    "Vector2", "Vector3", "Quaternion", "string"):
                refs.append(f)
        # Attributes immediately before the type declaration.
        pre = scan[max(0, start - 200):start]
        disallow_multiple = bool(re.search(
            r"\[DisallowMultipleComponent\]", pre))
        ctor_forbidden = []
        for f in fields:
            api = f.get("ctor_forbidden_api")
            if not api:
                continue
            abs_i = int(f.get("ctor_forbidden_abs")
                        or f.get("decl_abs") or 0)
            line = text.count("\n", 0, abs_i) + 1
            ctor_forbidden.append({
                "api": api,
                "field": f["name"],
                "line": line,
                "static": bool(f.get("static") or f.get("const")),
            })
        classes.append({
            "name": name, "kind": kind, "fields": fields,
            "methods": methods, "refs": refs,
            "path": path,
            "file_text": text,
            "disallow_multiple": disallow_multiple,
            "ctor_forbidden": ctor_forbidden,
        })
    return {
        "path": path,
        "apis": apis,
        "spawns": spawns,
        "uses_z": uses_z,
        "writes_pos": writes_pos,
        "writes_rot": writes_rot,
        "keyboard_keys": keyboard_keys,
        "getcomponent_types": getcomponent_types,
        "addcomponent_types": addcomponent_types,
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


def _parse_csharp_field_init(ty, raw):
    """Script field initializer → Python value, or None if unsupported."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if ty == "string":
        lit = _string_literal_value(raw)
        if lit is not None:
            return lit
        # Application.dataPath / persistentDataPath + "/rel" — pack-time bake.
        m = re.match(
            r"(?<![\w])(?:UnityEngine\.)?Application\.dataPath\s*\+\s*"
            r"(\"([^\"\\]|\\.)*\")\s*$",
            raw)
        if m:
            return {"kind": "dataPath+", "suffix": _string_literal_value(
                m.group(1))}
        if re.match(
                r"(?<![\w])(?:UnityEngine\.)?Application\.dataPath\s*$", raw):
            return {"kind": "dataPath"}
        m = re.match(
            r"(?<![\w])(?:UnityEngine\.)?Application\.persistentDataPath\s*\+\s*"
            r"(\"([^\"\\]|\\.)*\")\s*$",
            raw)
        if m:
            return {"kind": "persistentDataPath+", "suffix":
                    _string_literal_value(m.group(1))}
        if re.match(
                r"(?<![\w])(?:UnityEngine\.)?Application\.persistentDataPath\s*$",
                raw):
            return {"kind": "persistentDataPath"}
        return None
    if ty == "Vector2":
        m = re.match(
            r"new\s+Vector2\s*\(\s*(-?\d+(?:\.\d+)?)\s*[fF]?\s*,\s*"
            r"(-?\d+(?:\.\d+)?)\s*[fF]?\s*\)",
            raw)
        if m:
            return (float(m.group(1)), float(m.group(2)))
        return None
    if ty == "Vector3":
        m = re.match(
            r"new\s+Vector3\s*\(\s*(-?\d+(?:\.\d+)?)\s*[fF]?\s*,\s*"
            r"(-?\d+(?:\.\d+)?)\s*[fF]?\s*,\s*"
            r"(-?\d+(?:\.\d+)?)\s*[fF]?\s*\)",
            raw)
        if m:
            return (float(m.group(1)), float(m.group(2)), float(m.group(3)))
        return None
    if ty == "bool":
        if raw == "true":
            return 1
        if raw == "false":
            return 0
        return None
    if ty in ("float", "double"):
        m = re.match(
            r"(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*[fFdD]?", raw)
        if m:
            return float(m.group(1))
        return None
    if ty in ("int", "byte", "short", "uint", "long", "sbyte",
              "ushort", "ulong"):
        m = re.match(r"(-?\d+)", raw)
        if m:
            return int(m.group(1))
        return None
    return None


def _fields_in(body, bscan, body_abs=0):
    """Instance / static / const fields; methods (those with `(`) are skipped."""
    bscan = _blank_method_bodies(bscan)
    out = []
    for m in re.finditer(
            r"(?m)^[ \t]*(?:public|private|protected|internal)?"
            r"[ \t]*(?:static[ \t]+)?(?:const[ \t]+)?(?:readonly[ \t]+)?"
            r"([\w.<>]+)[ \t]+(\w+)[ \t]*(=|;)",
            bscan):
        # `int F(` is a method.
        tail = body[m.end(2):m.end(2) + 16]
        if "(" in tail.split(";")[0] and "=" not in tail.split(";")[0]:
            continue
        ty, name = m.group(1).strip(), m.group(2)
        if ty in ("if", "for", "return", "new", "const", "static"):
            continue
        decl = m.group(0)
        entry = {
            "ty": ty,
            "name": name,
            "static": bool(re.search(r"\bstatic\b", decl)),
            "const": bool(re.search(r"\bconst\b", decl)),
            "decl_abs": int(body_abs) + int(m.start()),
        }
        # Authored `float xSize = 1;` / `Vector2 multSize = new Vector2(1, 1);`
        if m.group(0).rstrip().endswith("="):
            rest = body[m.end():]
            semi = rest.find(";")
            if semi >= 0:
                init_src = rest[:semi]
                # Unity forbids Application.dataPath / persistentDataPath in
                # MonoBehaviour field initializers / .cctor (not Awake/Start).
                am = re.search(
                    r"(?<![\w])(?:UnityEngine\.)?Application\."
                    r"(dataPath|persistentDataPath)\b",
                    init_src)
                if am:
                    entry["ctor_forbidden_api"] = am.group(1)
                    entry["ctor_forbidden_abs"] = (
                        int(body_abs) + int(m.end()) + int(am.start()))
                default = _parse_csharp_field_init(ty, init_src)
                if default is not None:
                    entry["default"] = default
        out.append(entry)
    return out


def _member_init_default(cl, member_name):
    """Script field initializer for a packed member, or None."""
    for f in cl.get("fields") or []:
        if "default" not in f:
            continue
        if f["name"] == member_name:
            return f["default"]
        if f.get("ty") == "Vector2" and isinstance(f["default"], tuple):
            if member_name == f["name"] + "_x":
                return f["default"][0]
            if member_name == f["name"] + "_y":
                return f["default"][1]
        if f.get("ty") == "Vector3" and isinstance(f["default"], tuple):
            if member_name == f["name"] + "_x":
                return f["default"][0]
            if member_name == f["name"] + "_y":
                return f["default"][1]
            if member_name == f["name"] + "_z":
                return f["default"][2]
    return None


def _methods_in(body, bscan, body_abs=0):
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
        decl = m.group(0)
        out.append({
            "ret": m.group(1).strip(),
            "name": m.group(2),
            "args": m.group(3).strip(),
            "body": impl,
            "body_abs": int(body_abs) + int(m.end()),
            "src": src,
            "public": bool(re.search(r"\bpublic\b", decl)),
        })
    return out


# Unity messages we emit (Awake / OnEnable are skipped — run before first tick).
_UNITY_EMIT_MESSAGES = frozenset({
    "Start", "Update", "FixedUpdate", "LateUpdate",
    "OnDisable", "OnDestroy",
    "OnCollisionEnter2D", "OnCollisionStay2D", "OnCollisionExit2D",
    "OnTriggerEnter2D", "OnTriggerStay2D", "OnTriggerExit2D",
})


def _reachable_emit_methods(methods):
    """Methods to lower: Unity messages, public API, and private callees.

    Editor-only helpers (e.g. UpdateCanvas only called from OnValidate) stay out.
    """
    by = {}
    for m in methods or []:
        by[m["name"]] = m
    roots = set()
    for name, m in by.items():
        if name in ("Awake", "OnEnable", "OnValidate"):
            continue
        if name in _UNITY_EMIT_MESSAGES or m.get("public"):
            roots.add(name)
    reach = set(roots)
    queue = list(roots)
    while queue:
        name = queue.pop()
        body = (by.get(name) or {}).get("body") or ""
        for other in by:
            if other in reach:
                continue
            if re.search(r"(?<![\w.])%s\s*\(" % re.escape(other), body):
                reach.add(other)
                queue.append(other)
    return reach


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


# C# integer storage — runtime-mutated fields must not pack below this
# (e.g. `short framesLeft` assigned 100 must not become a 1-bit field).
_CS_INT_BITS = {
    "byte": 8, "sbyte": 8,
    "short": 16, "ushort": 16,
    "int": 32, "uint": 32,
    "long": 64, "ulong": 64,
}


def _assigned_int_seeds(fname, methods, fields):
    """Integer values assigned to fname in methods (consts / literals)."""
    consts = {}
    for f in fields or []:
        if f.get("const") and isinstance(f.get("default"), int):
            consts[f["name"]] = int(f["default"])
    seeds = []
    for m in methods or []:
        body = m.get("body") or ""
        for am in re.finditer(
                r"(?<![\w.])%s\s*=\s*([^;]+);" % re.escape(fname), body):
            rhs = am.group(1).strip()
            if rhs == fname:
                continue  # no-op `hp = hp;`
            if re.match(r"-?\d+$", rhs):
                seeds.append(int(rhs))
            elif rhs in consts:
                seeds.append(consts[rhs])
    return seeds


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
            writes[c["name"]] = (
                writes.get(c["name"], False)
                or a["writes_pos"]
                or a.get("writes_rot"))

    for cname, insts in by_class.items():
        # Authored Rigidbody / Animation integrates into transform — writable.
        if any(o.get("rigidbody2d") or o.get("rigidbody")
               or o.get("anim_player") for o in insts):
            writes[cname] = True

    # Live TRS consumers: TransformPoint / matrices / localPosition Vector3 fields.
    tp_classes = set()
    matrix_classes = set()
    local_pos_classes = set()
    for a in analyses:
        apis = a.get("apis") or ()
        for c in a["classes"]:
            if c["name"] not in by_class:
                continue
            if "transform.TransformPoint" in apis:
                tp_classes.add(c["name"])
                writes[c["name"]] = True
            if ("transform.worldToLocalMatrix" in apis
                    or "transform.localToWorldMatrix" in apis):
                matrix_classes.add(c["name"])
                writes[c["name"]] = True
            if "transform.localPosition" in apis:
                local_pos_classes.add(c["name"])
                writes[c["name"]] = True
            if "transform.localRotation" in apis:
                writes[c["name"]] = True

    # Vector3 field packing when localPosition round-trips a Vector3 member.
    vec3_pack_classes = tp_classes | local_pos_classes

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
        script_methods = []
        ctor_forbidden = []
        script_path = ""
        for a in analyses:
            for c in a["classes"]:
                if c["name"] == cname:
                    script_fields = c["fields"]
                    script_methods = c["methods"]
                    ctor_forbidden = list(c.get("ctor_forbidden") or [])
                    script_path = c.get("path") or a.get("path") or ""
        for f in script_fields:
            if f.get("static") or f.get("const"):
                continue  # class-level; emitted separately from instance arrays
            field_tys[f["name"]] = f["ty"]
        for o in insts:
            for k in o["fields"]:
                # Vector2 components are packed via the Vector2 script field.
                if k.endswith("_x") or k.endswith("_y"):
                    base = k[:-2]
                    if any(f["name"] == base and f.get("ty") == "Vector2"
                           for f in script_fields):
                        continue
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
            if ty == "string":
                # Instance strings are not packed yet (static strings are).
                continue
            if ty in ("StreamWriter", "StreamReader"):
                # Text streams are FILE* class/static fields, not instance slots.
                continue
            if ty == "Vector2":
                members.append((fname + "_x", "float", 32, "f32"))
                members.append((fname + "_y", "float", 32, "f32"))
                continue
            if ty == "Vector3":
                # Pack when TransformPoint / localPosition needs field points.
                if cname in vec3_pack_classes:
                    members.append((fname + "_x", "float", 32, "f32"))
                    members.append((fname + "_y", "float", 32, "f32"))
                    members.append((fname + "_z", "float", 32, "f32"))
                continue  # otherwise transform owns position; full Vector3 later
            if ty == "Transform":
                # Resolved via object_refs → target class/inst (SetWorldScale).
                continue
            if ty in _UI_COMPONENT_FIELD_TYPES:
                # Authored uGUI / TMP refs — drawn from scene, not packed MB idx.
                continue
            if "[" in ty or ty == "AudioClip":
                # Arrays / AudioClip assets — opaque handles later; skip pack.
                continue
            if ty in ("int", "byte", "short", "uint"):
                vals = [o["fields"][fname] for o in insts if fname in o["fields"]]
                vals.extend(_assigned_int_seeds(
                    fname, script_methods, script_fields))
                type_bits = _CS_INT_BITS.get(ty, 32)
                # No scene/seed values → C# width (not phantom [0] → 1 bit).
                # Seeds from `framesLeft = FRAME_CNT` widen counters correctly.
                if not vals:
                    w = type_bits
                else:
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
        vec3_fields = [f["name"] for f in script_fields
                       if f["ty"] == "Vector3" and cname in vec3_pack_classes]
        class_consts = [f for f in script_fields
                        if f.get("const") or f.get("static")]
        # Do not bake Application.* paths used in illegal field initializers.
        if ctor_forbidden:
            forbid_names = {x["field"] for x in ctor_forbidden}
            class_consts = [f for f in class_consts
                            if f["name"] not in forbid_names]
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
            "vec3_fields": vec3_fields,
            "class_consts": class_consts,
            "ctor_forbidden": ctor_forbidden,
            "script_path": script_path,
        }
    live_rot = set(tp_classes) | set(matrix_classes)
    for a in analyses:
        if a.get("writes_rot"):
            for c in a["classes"]:
                if c["name"] in plans:
                    live_rot.add(c["name"])
        apis = a.get("apis") or ()
        if "transform.localRotation" in apis:
            for c in a["classes"]:
                if c["name"] in plans:
                    live_rot.add(c["name"])
    return {
        "two_d": two_d,
        "spawn": spawn,
        "classes": plans,
        "live_rot_classes": sorted(live_rot),
        "transform_point_classes": sorted(tp_classes),
        "transform_matrix_classes": sorted(matrix_classes),
        "local_position_classes": sorted(local_pos_classes),
    }


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
    want_live_rot = bool(plan.get("live_rot_classes"))
    want_transform_matrix = bool(plan.get("transform_matrix_classes"))
    want_quat_angle = "Quaternion.Angle" in used_apis
    if ("File.WriteAllBytes" in used_apis
            or "File.ReadAllBytes" in used_apis):
        if "File.WriteAllBytes" in used_apis:
            plan["byte_array_lits"] = _collect_byte_array_lits(
                plan, analyses)
        else:
            plan["byte_array_lits"] = []
        plan["_byte_array_lit_i"] = [0]
    getcomponent_types = set()
    for a in analyses:
        getcomponent_types |= set(a.get("getcomponent_types") or [])
    rb2d_list = plan.get("rigidbody2d") or []
    rb3d_list = plan.get("rigidbody") or []
    col2d_list = plan.get("collider2d") or []
    col3d_list = plan.get("collider3d") or []
    anim_plan = plan.get("animation") or {}
    anim_players = anim_plan.get("players") or []
    anim_clips = anim_plan.get("clips") or []
    anim_keys = anim_plan.get("keys") or []
    add_types = set(plan.get("addcomponent_types") or [])
    add_budget = plan.get("addcomponent_budget") or {}
    disallow_multi = set(plan.get("disallow_multiple_types")
                         or _DISALLOW_MULTIPLE_BUILTINS)
    go_has_sprite = set(plan.get("go_has_sprite") or [])
    want_rb2d = (
        bool(rb2d_list)
        or "Rigidbody2D" in getcomponent_types
        or "Rigidbody2D" in used_apis
        or "Rigidbody2D" in add_types)
    want_rb3d = (
        bool(rb3d_list)
        or "Rigidbody" in getcomponent_types
        or "Rigidbody" in add_types)
    want_col2d = bool(col2d_list) or bool(
        add_types & {"BoxCollider2D", "CircleCollider2D"})
    want_col3d = bool(col3d_list) or bool(
        add_types & {"BoxCollider", "SphereCollider"})
    want_anim = bool(anim_players)
    want_add_camera = "Camera" in add_types
    want_add_light = "Light" in add_types
    want_add_sprite = "SpriteRenderer" in add_types
    want_add_audio = (
        "AudioSource" in add_types
        or "AudioSource" in getcomponent_types
        or bool(plan.get("audiosources")))
    want_add_any = bool(add_types)
    want_phys = "Physics2D.gravity" in used_apis or want_rb2d
    want_phys3 = "Physics.gravity" in used_apis or want_rb3d
    want_input = bool(used_apis & _WANT_INPUT)
    want_keyboard = "Keyboard.current" in used_apis
    keyboard_keys = set()
    for a in analyses:
        keyboard_keys |= set(a.get("keyboard_keys") or [])
    want_ambient = "RenderSettings.ambientLight" in used_apis or want_add_light
    want_log = bool(used_apis & {"Debug.Log", "print"})
    want_console = "Console.WriteLine" in used_apis
    want_str_plus = "string.+" in used_apis
    want_find = "GameObject.Find" in used_apis
    want_transform_find = "transform.Find" in used_apis
    want_transform_parent = "transform.parent" in used_apis
    want_transform_go = "transform.gameObject" in used_apis
    want_set_parent = "transform.SetParent" in used_apis
    want_get_sibling = "transform.GetSiblingIndex" in used_apis
    want_getcomponent = "GetComponent" in used_apis
    want_data_path = "Application.dataPath" in used_apis
    want_persistent_data_path = "Application.persistentDataPath" in used_apis
    want_app_is_editor = "Application.isEditor" in used_apis
    want_app_is_playing = "Application.isPlaying" in used_apis
    want_app_open_url = "Application.OpenURL" in used_apis
    want_app_product_name = "Application.productName" in used_apis
    want_app_quit = "Application.Quit" in used_apis
    want_file_write = "File.WriteAllText" in used_apis
    want_file_append = "File.AppendAllText" in used_apis
    want_file_write_bytes = "File.WriteAllBytes" in used_apis
    want_file_read_bytes = "File.ReadAllBytes" in used_apis
    want_file_bytes = want_file_write_bytes or want_file_read_bytes
    want_file_exists = "File.Exists" in used_apis
    want_file_delete = "File.Delete" in used_apis
    want_file_create_text = "File.CreateText" in used_apis
    want_file_open_text = "File.OpenText" in used_apis
    want_file_copy = "File.Copy" in used_apis
    want_file_text_stream = want_file_create_text or want_file_open_text
    want_file_write_ops = (
        want_file_write or want_file_append or want_file_write_bytes
        or want_file_create_text or want_file_copy)
    want_file_io = (
        want_file_write_ops or want_file_exists or want_file_read_bytes
        or want_file_delete or want_file_text_stream or want_file_copy)
    want_destroy = "Object.Destroy" in used_apis
    ui_buttons = plan.get("ui_buttons") or []
    want_ui = bool(ui_buttons)
    want_go_tables = (
        want_find or want_transform_find or want_transform_parent
        or want_transform_go or want_set_parent or want_get_sibling
        or want_getcomponent
        or want_rb2d or want_rb3d or want_add_any or want_ui or want_destroy)
    want_ctor_forbidden = any(
        bool(cl.get("ctor_forbidden"))
        for cl in plan["classes"].values())
    light_n = int(plan.get("light_count") or 0)
    light_cap = light_n + int(add_budget.get("Light") or 0)
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    p("/* generated by tools/unity_pack.py — do not edit */")
    if soa:
        p("/* layout: SoA positions (contiguous float tables for GPU upload) */")
    p("#include <stdint.h>")
    if (want_math or want_col2d or want_col3d or want_anim or want_live_rot
            or want_transform_matrix or want_quat_angle):
        p("#include <math.h>")
    if (want_input or want_log or want_find or want_transform_find
            or want_set_parent or want_get_sibling
            or want_add_any or want_data_path or want_persistent_data_path
            or want_file_io or soa):
        p("#include <string.h>")
    if (want_log or want_console or want_str_plus or want_add_any
            or want_file_io or want_go_tables or want_ctor_forbidden
            or want_app_open_url):
        p("#include <stdio.h>")
    want_draw_sort = False
    for cl in plan["classes"].values():
        for o in cl["instances"]:
            sp = o.get("sprite")
            if sp and sp.get("enabled", 1) and "tex_id" in sp:
                want_draw_sort = True
                break
        if want_draw_sort:
            break
    if (want_log or want_draw_sort or want_data_path
            or want_persistent_data_path or want_file_io or want_go_tables
            or want_app_open_url):
        p("#include <stdlib.h>")
    if want_go_tables:
        p("#include <setjmp.h>")
    if want_log or want_file_write_ops:
        # Host gcc creates dirs; crust/shivyc has no errno/sys/stat,
        # so CRUST_NO_POSIX_MKDIR skips mkdir and fopen falls back.
        p("#ifndef CRUST_NO_POSIX_MKDIR")
        p("#include <errno.h>")
        p("#ifdef _WIN32")
        p("#include <direct.h>")
        p("#define ENGINE_MKDIR(p) _mkdir(p)")
        p("#else")
        p("#include <sys/stat.h>")
        p("#define ENGINE_MKDIR(p) mkdir((p), 0755)")
        p("#endif")
        p("#endif")
    p("")
    p("/* Types first, then every global. C forbids `extern T a[N]` while")
    p("   T is incomplete, so the arrays wait until the structs exist;")
    p("   the names are all listed here as comments so data.c and any")
    p("   group can find them. */")
    if want_transform_matrix:
        p("/* Unity Matrix4x4 — column-major; TRS from live Transform. */")
        p("typedef struct Matrix4x4 {")
        p("    float m00, m01, m02, m03;")
        p("    float m10, m11, m12, m13;")
        p("    float m20, m21, m22, m23;")
        p("    float m30, m31, m32, m33;")
        p("} Matrix4x4;")
        p("")
    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        p("typedef struct %s %s;" % (idn, idn))
    p("extern float Time_deltaTime;")
    if "Time.time" in used_apis:
        p("extern float Time_time;")
    p("extern float Time_fixedDeltaTime;")
    p("extern int Screen_width;")
    p("extern int Screen_height;")
    p("extern int Screen_fullScreen;")
    p("extern int Screen_fullScreenNative;")
    p("extern int Screen_maximized;")
    if want_phys:
        p("extern float Physics2D_gravity_x;")
        p("extern float Physics2D_gravity_y;")
    if want_phys3:
        p("extern float Physics_gravity_x;")
        p("extern float Physics_gravity_y;")
        p("extern float Physics_gravity_z;")
    if want_ambient:
        p("extern float RenderSettings_ambient_r;")
        p("extern float RenderSettings_ambient_g;")
        p("extern float RenderSettings_ambient_b;")
    if plan.get("camera"):
        p("extern float Camera_main_pos_x;")
        p("extern float Camera_main_pos_y;")
        p("extern float Camera_main_pos_z;")
        p("extern float Camera_main_orthographicSize;")
        p("extern float Camera_main_nearClipPlane;")
        p("extern float Camera_main_farClipPlane;")
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
    if want_ui:
        p("extern float engine_pointer_x; /* screen px, origin bottom-left */")
        p("extern float engine_pointer_y;")
        p("extern int engine_pointer_down;")
    if light_cap:
        p("extern int _Light_count;")
        p("extern float _Light_intensity[%d];" % max(1, light_cap))
        p("extern float _Light_color_r[%d];" % max(1, light_cap))
        p("extern float _Light_color_g[%d];" % max(1, light_cap))
        p("extern float _Light_color_b[%d];" % max(1, light_cap))
    rb2d_cap = len(rb2d_list) + int(add_budget.get("Rigidbody2D") or 0)
    rb3d_cap = len(rb3d_list) + int(add_budget.get("Rigidbody") or 0)
    if want_rb2d:
        n2 = max(1, rb2d_cap if rb2d_cap else len(rb2d_list) or 1)
        p("extern int _Rigidbody2D_count;")
        p("extern float _Rigidbody2D_vel_x[%d];" % n2)
        p("extern float _Rigidbody2D_vel_y[%d];" % n2)
        p("extern float _Rigidbody2D_gravity_scale[%d];" % n2)
        p("extern float _Rigidbody2D_linear_damping[%d];" % n2)
        p("extern float _Rigidbody2D_mass[%d];" % n2)
        p("extern int _Rigidbody2D_body_type[%d];" % n2)
        p("extern int _Rigidbody2D_owner_class[%d];" % n2)
        p("extern int _Rigidbody2D_owner_inst[%d];" % n2)
    if want_rb3d:
        n3 = max(1, rb3d_cap if rb3d_cap else len(rb3d_list) or 1)
        p("extern int _Rigidbody_count;")
        p("extern float _Rigidbody_vel_x[%d];" % n3)
        p("extern float _Rigidbody_vel_y[%d];" % n3)
        p("extern float _Rigidbody_vel_z[%d];" % n3)
        p("extern float _Rigidbody_mass[%d];" % n3)
        p("extern float _Rigidbody_drag[%d];" % n3)
        p("extern int _Rigidbody_use_gravity[%d];" % n3)
        p("extern int _Rigidbody_owner_class[%d];" % n3)
        p("extern int _Rigidbody_owner_inst[%d];" % n3)
    if want_col2d:
        nc = max(1, len(col2d_list))
        p("extern const int _Collider2D_count;")
        p("extern const int _Collider2D_kind[%d]; /* 0 box 1 circle */" % nc)
        p("extern const int _Collider2D_is_trigger[%d];" % nc)
        p("extern const int _Collider2D_body_type[%d]; /* 0 dyn 1 kin 2 static */"
          % nc)
        p("extern const int _Collider2D_owner_class[%d];" % nc)
        p("extern const int _Collider2D_owner_inst[%d];" % nc)
        p("extern const int _Collider2D_rb2d[%d]; /* -1 if none */" % nc)
        p("extern const float _Collider2D_ox[%d];" % nc)
        p("extern const float _Collider2D_oy[%d];" % nc)
        p("extern const float _Collider2D_hw[%d];" % nc)
        p("extern const float _Collider2D_hh[%d];" % nc)
        p("extern const float _Collider2D_cos[%d];" % nc)
        p("extern const float _Collider2D_sin[%d];" % nc)
        p("extern const float _Collider2D_friction[%d];" % nc)
        p("extern const float _Collider2D_bounciness[%d];" % nc)
        p("extern const int _Collider2D_friction_combine[%d];" % nc)
        p("extern const int _Collider2D_bounce_combine[%d];" % nc)
    if want_col3d:
        n3c = max(1, len(col3d_list))
        p("extern const int _Collider3D_count;")
        p("extern const int _Collider3D_kind[%d]; /* 0 box 1 sphere */" % n3c)
        p("extern const int _Collider3D_is_trigger[%d];" % n3c)
        p("extern const int _Collider3D_body_type[%d]; /* 0 dyn 2 static */"
          % n3c)
        p("extern const int _Collider3D_owner_class[%d];" % n3c)
        p("extern const int _Collider3D_owner_inst[%d];" % n3c)
        p("extern const int _Collider3D_rb3d[%d]; /* -1 if none */" % n3c)
        p("extern const float _Collider3D_ox[%d];" % n3c)
        p("extern const float _Collider3D_oy[%d];" % n3c)
        p("extern const float _Collider3D_oz[%d];" % n3c)
        p("extern const float _Collider3D_hw[%d];" % n3c)
        p("extern const float _Collider3D_hh[%d];" % n3c)
        p("extern const float _Collider3D_hd[%d];" % n3c)
        p("extern const float _Collider3D_dynamic_friction[%d];" % n3c)
        p("extern const float _Collider3D_static_friction[%d];" % n3c)
        p("extern const float _Collider3D_bounciness[%d];" % n3c)
        p("extern const int _Collider3D_friction_combine[%d];" % n3c)
        p("extern const int _Collider3D_bounce_combine[%d];" % n3c)
    if want_anim and anim_players:
        np = max(1, len(anim_players))
        nc = max(1, len(anim_clips))
        nk = max(1, len(anim_keys))
        p("extern const int _AnimPlayer_count;")
        p("extern int _AnimPlayer_playing[%d];" % np)
        p("extern float _AnimPlayer_time[%d];" % np)
        p("extern const float _AnimPlayer_speed[%d];" % np)
        p("extern const int _AnimPlayer_loop[%d];" % np)
        p("extern const int _AnimPlayer_clip[%d];" % np)
        p("extern const int _AnimPlayer_owner_class[%d];" % np)
        p("extern const int _AnimPlayer_owner_inst[%d];" % np)
        p("extern const float _AnimPlayer_rest_x[%d];" % np)
        p("extern const float _AnimPlayer_rest_y[%d];" % np)
        p("extern const float _AnimPlayer_rest_z[%d];" % np)
        p("extern const int _AnimClip_count;")
        p("extern const float _AnimClip_length[%d];" % nc)
        p("extern const int _AnimClip_key_begin[%d];" % nc)
        p("extern const int _AnimClip_key_count[%d];" % nc)
        p("extern const int _AnimKey_count;")
        p("extern const float _AnimKey_t[%d];" % nk)
        p("extern const float _AnimKey_x[%d];" % nk)
        p("extern const float _AnimKey_y[%d];" % nk)
        p("extern const float _AnimKey_z[%d];" % nk)
        anim_skeys = anim_plan.get("sprite_keys") or []
        anim_sbinds = anim_plan.get("sprite_binds") or []
        if anim_skeys or anim_sbinds:
            nsk = max(1, len(anim_skeys))
            nsb = max(1, len(anim_sbinds))
            p("extern const int _AnimPlayer_sprite_bind_begin[%d];" % np)
            p("extern const int _AnimPlayer_sprite_bind_count[%d];" % np)
            p("extern const int _AnimSpriteBind_count;")
            p("extern const int _AnimSpriteBind_target_class[%d];" % nsb)
            p("extern const int _AnimSpriteBind_target_inst[%d];" % nsb)
            p("extern const int _AnimSpriteBind_key_begin[%d];" % nsb)
            p("extern const int _AnimSpriteBind_key_count[%d];" % nsb)
            p("extern const int _AnimSpriteKey_count;")
            p("extern const float _AnimSpriteKey_t[%d];" % nsk)
            p("extern const int _AnimSpriteKey_tex[%d];" % nsk)
            p("extern const float _AnimSpriteKey_hw[%d];" % nsk)
            p("extern const float _AnimSpriteKey_hh[%d];" % nsk)
    mutable_spr = set(plan.get("sprite_draw_mutable") or [])
    for cname in sorted(mutable_spr):
        if cname not in plan["classes"]:
            continue
        idn = _c_ident(cname)
        n = max(1, plan["classes"][cname]["n"])
        p("extern int _%s_draw_tex[%d];" % (idn, n))
        p("extern float _%s_draw_hw[%d];" % (idn, n))
        p("extern float _%s_draw_hh[%d];" % (idn, n))
    for cname in sorted(plan.get("live_scale_classes") or []):
        if cname not in plan["classes"]:
            continue
        idn = _c_ident(cname)
        n = max(1, plan["classes"][cname]["n"])
        p("extern float _%s_scale_x[%d];" % (idn, n))
        p("extern float _%s_scale_y[%d];" % (idn, n))
    for cname in sorted(plan.get("live_rot_classes") or []):
        if cname not in plan["classes"]:
            continue
        idn = _c_ident(cname)
        n = max(1, plan["classes"][cname]["n"])
        p("extern float _%s_rot_x[%d];" % (idn, n))
        p("extern float _%s_rot_y[%d];" % (idn, n))
        p("extern float _%s_rot_z[%d];" % (idn, n))
        p("extern float _%s_rot_w[%d];" % (idn, n))
        p("extern float _%s_rot_m00[%d];" % (idn, n))
        p("extern float _%s_rot_m01[%d];" % (idn, n))
        p("extern float _%s_rot_m10[%d];" % (idn, n))
        p("extern float _%s_rot_m11[%d];" % (idn, n))
    # Transform field → target class/inst (SetWorldScale).
    for (oc, fname), row in sorted(
            (plan.get("transform_field_targets") or {}).items()):
        if oc not in plan["classes"]:
            continue
        idn = _c_ident(oc)
        n = max(1, len(row) or 1)
        p("extern const int _%s_%s_target_class[%d];" % (idn, fname, n))
        p("extern const int _%s_%s_target_inst[%d];" % (idn, fname, n))
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
        mb_budget = int((plan.get("addcomponent_budget") or {}).get(cname) or 0)
        cap = cl["n"] + mb_budget
        p("extern %s _%s_inst_array[%d];" % (idn, idn, max(1, cap)))
        if mb_budget:
            p("extern int _%s_inst_count;" % idn)
        else:
            p("extern const int _%s_inst_count;" % idn)
        if cl.get("soa_dims"):
            p("extern float _%s_pos[%d][%d];" % (
                idn, max(1, cap), cl["soa_dims"]))
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
    if want_str_plus:
        # C# "" + 1 → "1"; C's ""+1 is pointer arithmetic (often prints garbage).
        # Ring of buffers: nested _str_plus_*(prev, x) must not snprintf into
        # the same slot it reads, and sibling args (path + contents) must stay
        # live until the callee returns — two slots are not enough for
        # AppendAllText(pathExpr, contentExpr) where both are concatenations.
        p("/* C# string + value (not C pointer arithmetic) */")
        p("static char _engine_str_buf[8][512];")
        p("static int _engine_str_which;")
        p("static const char *_str_plus_i(const char *a, int b) {")
        p("    char *out = _engine_str_buf[_engine_str_which++ & 7];")
        p("    snprintf(out, sizeof _engine_str_buf[0], \"%s%d\",")
        p("             a ? a : \"\", b);")
        p("    return out;")
        p("}")
        p("static const char *_str_plus_f(const char *a, float b) {")
        p("    char *out = _engine_str_buf[_engine_str_which++ & 7];")
        p("    snprintf(out, sizeof _engine_str_buf[0], \"%s%g\",")
        p("             a ? a : \"\", (double)b);")
        p("    return out;")
        p("}")
        p("static const char *_str_plus_c(const char *a, char b) {")
        p("    char *out = _engine_str_buf[_engine_str_which++ & 7];")
        p("    snprintf(out, sizeof _engine_str_buf[0], \"%s%c\",")
        p("             a ? a : \"\", b);")
        p("    return out;")
        p("}")
        p("static const char *_str_plus_s(const char *a, const char *b) {")
        p("    char *out = _engine_str_buf[_engine_str_which++ & 7];")
        p("    snprintf(out, sizeof _engine_str_buf[0], \"%s%s\",")
        p("             a ? a : \"\", b ? b : \"\");")
        p("    return out;")
        p("}")
        p("/* Call sites pick _str_plus_{i,f,c,s} at rewrite (no C11 generics). */")
        p("")
    if want_data_path:
        data_path = plan.get("data_path") or ""
        p("/* Application.dataPath — Assets folder of the packed project */")
        p("static const char _engine_data_path[] = %s;" % _c_string(data_path))
        p("static const char *Application_dataPath(void) {")
        p("    return _engine_data_path;")
        p("}")
        p("const char *engine_data_path(void) { return _engine_data_path; }")
        p("")
    if want_persistent_data_path:
        pp = plan.get("persistent_data_path") or ""
        p("/* Application.persistentDataPath — Unity company/product save dir */")
        p("static const char _engine_persistent_data_path[] = %s;"
          % _c_string(pp))
        p("static const char *Application_persistentDataPath(void) {")
        p("    return _engine_persistent_data_path;")
        p("}")
        p("const char *engine_persistent_data_path(void) {")
        p("    return _engine_persistent_data_path;")
        p("}")
        p("")
    if want_app_is_editor:
        p("/* Application.isEditor — packed player is never the Unity Editor */")
        p("static int Application_isEditor(void) { return 0; }")
        p("")
    if want_app_is_playing:
        p("/* Application.isPlaying — true while the packed player runs */")
        p("static int Application_isPlaying(void) { return 1; }")
        p("")
    if want_app_product_name:
        product = plan.get("product_name") or "Player"
        p("/* Application.productName — ProjectSettings productName */")
        p("static const char _engine_app_product_name[] = %s;"
          % _c_string(product))
        p("static const char *Application_productName(void) {")
        p("    return _engine_app_product_name;")
        p("}")
        p("")
    if want_app_open_url:
        p("/* Application.OpenURL — python3 webbrowser via system(3) */")
        p("static void Application_OpenURL(const char *url) {")
        p("    char cmd[4096];")
        p("    char py[2048];")
        p("    int i, j;")
        p("    if (!url || !url[0]) return;")
        p("    j = 0;")
        p("    py[j++] = '\\'';")
        p("    for (i = 0; url[i] && j + 2 < (int)sizeof py; i++) {")
        p("        if (url[i] == '\\'' || url[i] == '\\\\') {")
        p("            py[j++] = '\\\\';")
        p("            py[j++] = url[i];")
        p("        } else {")
        p("            py[j++] = url[i];")
        p("        }")
        p("    }")
        p("    py[j++] = '\\'';")
        p("    py[j] = 0;")
        p("    snprintf(cmd, sizeof cmd,")
        p("        \"python3 -c \\\"import webbrowser; webbrowser.open(%s)\\\"\",")
        p("        py);")
        p("    (void)system(cmd);")
        p("}")
        p("")
    if want_app_quit:
        p("/* Application.Quit — host polls engine_wants_quit() */")
        p("static int _engine_quit;")
        p("static void Application_Quit(int exit_code) {")
        p("    (void)exit_code;")
        p("    _engine_quit = 1;")
        p("}")
        p("int engine_wants_quit(void) { return _engine_quit; }")
        p("")
    else:
        p("int engine_wants_quit(void) { return 0; }")
        p("")
    if want_destroy:
        # Destroy(gameObject) — mark GO; Tick skips destroyed instances.
        go_n = max(1, len(plan.get("go_names") or []))
        p("/* Destroy(gameObject) — stop Update; no pool free */")
        p("static int _engine_go_destroyed[%d];" % go_n)
        p("static void Object_Destroy(int go) {")
        p("    if (go >= 0 && go < %d) _engine_go_destroyed[go] = 1;" % go_n)
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
        p("#ifndef CRUST_NO_POSIX_MKDIR")
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
        p("#endif")
        p("")
        p("static const char *_engine_default_log_path(void) {")
        p("    const char *home;")
        p("#ifndef CRUST_NO_POSIX_MKDIR")
        p("    char dir[1024];")
        p("    int n, i;")
        p("#else")
        p("    int n;")
        p("#endif")
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
        p("#ifndef CRUST_NO_POSIX_MKDIR")
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
        p("#endif")
        p("    return _engine_log_default;")
        p("}")
        p("")
        p("void engine_set_log_file(const char *path) {")
        p("    if (_engine_log_fp && !_engine_log_stdout) {")
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
        p("        if (!_engine_log_fp) {")
        p("            _engine_log_stdout = 1;")
        p("            return stdout;")
        p("        }")
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
        p("/* Call sites pick Debug_Log_{i,f,s} at rewrite (no C11 generics). */")
        p("")
    else:
        p("void engine_set_log_file(const char *path) { (void)path; }")
        p("const char *engine_console_log_path(void) { return \"\"; }")
        p("")
    if not want_data_path:
        p("const char *engine_data_path(void) { return \"\"; }")
        p("")
    if not want_persistent_data_path:
        p("const char *engine_persistent_data_path(void) { return \"\"; }")
        p("")
    if want_file_write_ops or want_file_read_bytes:
        if want_file_bytes:
            p("/* System.IO byte[] → ByteArray (WriteAllBytes / ReadAllBytes) */")
            p("typedef struct {")
            p("    const unsigned char *data;")
            p("    int length;")
            p("} ByteArray;")
            p("")
        if want_file_write_ops:
            p("/* System.IO.File.WriteAllText / AppendAllText / WriteAllBytes */")
            if not want_log:
                p("#ifndef CRUST_NO_POSIX_MKDIR")
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
                p("#endif")
                p("")
            p("static void File_WriteContents(const char *path,")
            p("                               const char *contents,")
            p("                               const char *mode) {")
            p("    FILE *fp;")
            p("#ifndef CRUST_NO_POSIX_MKDIR")
            p("    char dir[1024];")
            p("    int n, i;")
            p("    if (path && path[0]) {")
            p("        n = (int)strlen(path);")
            p("        if (n > 0 && (size_t)n < sizeof dir) {")
            p("            for (i = 0; i < n; i++) dir[i] = path[i];")
            p("            dir[n] = 0;")
            p("            for (i = n - 1; i >= 0; i--) {")
            p("#ifdef _WIN32")
            p("                if (dir[i] == '/' || dir[i] == '\\\\') {")
            p("                    dir[i] = 0;")
            p("                    if (dir[0]) _engine_mkdir_p(dir);")
            p("                    break;")
            p("                }")
            p("#else")
            p("                if (dir[i] == '/') {")
            p("                    dir[i] = 0;")
            p("                    if (dir[0]) _engine_mkdir_p(dir);")
            p("                    break;")
            p("                }")
            p("#endif")
            p("            }")
            p("        }")
            p("    }")
            p("#endif")
            p("    fp = fopen(path ? path : \"\", mode ? mode : \"w\");")
            p("    if (!fp) return;")
            p("    if (contents) fputs(contents, fp);")
            p("    fclose(fp);")
            p("}")
            if want_file_write:
                p("static void File_WriteAllText(const char *path,")
                p("                              const char *contents) {")
                p("    File_WriteContents(path, contents, \"w\");")
                p("}")
            if want_file_append:
                p("static void File_AppendAllText(const char *path,")
                p("                               const char *contents) {")
                p("    File_WriteContents(path, contents, \"a\");")
                p("}")
            if want_file_write_bytes:
                p("static void File_WriteAllBytes(const char *path, ByteArray bytes) {")
                p("    FILE *fp;")
                p("#ifndef CRUST_NO_POSIX_MKDIR")
                p("    char dir[1024];")
                p("    int n, i;")
                p("    if (path && path[0]) {")
                p("        n = (int)strlen(path);")
                p("        if (n > 0 && (size_t)n < sizeof dir) {")
                p("            for (i = 0; i < n; i++) dir[i] = path[i];")
                p("            dir[n] = 0;")
                p("            for (i = n - 1; i >= 0; i--) {")
                p("#ifdef _WIN32")
                p("                if (dir[i] == '/' || dir[i] == '\\\\') {")
                p("                    dir[i] = 0;")
                p("                    if (dir[0]) _engine_mkdir_p(dir);")
                p("                    break;")
                p("                }")
                p("#else")
                p("                if (dir[i] == '/') {")
                p("                    dir[i] = 0;")
                p("                    if (dir[0]) _engine_mkdir_p(dir);")
                p("                    break;")
                p("                }")
                p("#endif")
                p("            }")
                p("        }")
                p("    }")
                p("#endif")
                p("    fp = fopen(path ? path : \"\", \"wb\");")
                p("    if (!fp) return;")
                p("    if (bytes.data && bytes.length > 0)")
                p("        fwrite(bytes.data, 1, (size_t)bytes.length, fp);")
                p("    fclose(fp);")
                p("}")
                # Literal helpers collected while lowering method bodies.
                for bi, nums in enumerate(plan.get("byte_array_lits") or []):
                    if not nums:
                        p("static unsigned char _engine_ba_%d_data[1] = { 0 };"
                          % bi)
                        p("static ByteArray _engine_ba_%d(void) {" % bi)
                        p("    ByteArray b;")
                        p("    b.data = _engine_ba_%d_data;" % bi)
                        p("    b.length = 0;")
                        p("    return b;")
                        p("}")
                    else:
                        p("static unsigned char _engine_ba_%d_data[%d] = { %s };"
                          % (bi, len(nums),
                             ", ".join(str(int(x) & 0xFF) for x in nums)))
                        p("static ByteArray _engine_ba_%d(void) {" % bi)
                        p("    ByteArray b;")
                        p("    b.data = _engine_ba_%d_data;" % bi)
                        p("    b.length = %d;" % len(nums))
                        p("    return b;")
                        p("}")
        if want_file_read_bytes:
            p("/* System.IO.File.ReadAllBytes — malloc buffer (no free). */")
            p("static ByteArray File_ReadAllBytes(const char *path) {")
            p("    ByteArray out;")
            p("    FILE *fp;")
            p("    unsigned char chunk[4096];")
            p("    unsigned char *buf;")
            p("    size_t cap, len, n;")
            p("    out.data = 0;")
            p("    out.length = 0;")
            p("    if (!path || !path[0]) return out;")
            p("    fp = fopen(path, \"rb\");")
            p("    if (!fp) return out;")
            p("    cap = 4096;")
            p("    len = 0;")
            p("    buf = (unsigned char *)malloc(cap);")
            p("    if (!buf) { fclose(fp); return out; }")
            p("    for (;;) {")
            p("        n = fread(chunk, 1, sizeof chunk, fp);")
            p("        if (n == 0) break;")
            p("        if (len + n > cap) {")
            p("            cap = cap + n + 4096;")
            p("            {")
            p("                unsigned char *nb = (unsigned char *)realloc(buf, cap);")
            p("                if (!nb) { free(buf); fclose(fp); return out; }")
            p("                buf = nb;")
            p("            }")
            p("        }")
            p("        memcpy(buf + len, chunk, n);")
            p("        len += n;")
            p("    }")
            p("    fclose(fp);")
            p("    out.data = buf;")
            p("    out.length = (int)len;")
            p("    return out;")
            p("}")
        p("")
    if want_file_exists:
        # fopen probe — no unistd/access (crust subset); dirs fail like .NET.
        p("/* System.IO.File.Exists */")
        p("static int File_Exists(const char *path) {")
        p("    FILE *fp;")
        p("    if (!path || !path[0]) return 0;")
        p("    fp = fopen(path, \"rb\");")
        p("    if (!fp) return 0;")
        p("    fclose(fp);")
        p("    return 1;")
        p("}")
        p("")
    if want_file_delete:
        # remove(3) — missing path is a no-op (packed player; no throw).
        p("/* System.IO.File.Delete */")
        p("static void File_Delete(const char *path) {")
        p("    if (!path || !path[0]) return;")
        p("    (void)remove(path);")
        p("}")
        p("")
    if want_file_copy:
        # Copy(src, dest[, overwrite]) — fread/fwrite; no throw.
        p("/* System.IO.File.Copy — fread/fwrite (+ mkdir dest parent) */")
        p("static void File_Copy(const char *src, const char *dst,")
        p("                      int overwrite) {")
        p("    FILE *in;")
        p("    FILE *out;")
        p("    unsigned char buf[4096];")
        p("    size_t n;")
        p("    if (!src || !src[0] || !dst || !dst[0]) return;")
        p("    if (!overwrite) {")
        p("        out = fopen(dst, \"rb\");")
        p("        if (out) { fclose(out); return; }")
        p("    }")
        p("#ifndef CRUST_NO_POSIX_MKDIR")
        p("    {")
        p("        char dir[1024];")
        p("        int dn, i;")
        p("        dn = (int)strlen(dst);")
        p("        if (dn > 0 && (size_t)dn < sizeof dir) {")
        p("            for (i = 0; i < dn; i++) dir[i] = dst[i];")
        p("            dir[dn] = 0;")
        p("            for (i = dn - 1; i >= 0; i--) {")
        p("#ifdef _WIN32")
        p("                if (dir[i] == '/' || dir[i] == '\\\\') {")
        p("                    dir[i] = 0;")
        p("                    if (dir[0]) _engine_mkdir_p(dir);")
        p("                    break;")
        p("                }")
        p("#else")
        p("                if (dir[i] == '/') {")
        p("                    dir[i] = 0;")
        p("                    if (dir[0]) _engine_mkdir_p(dir);")
        p("                    break;")
        p("                }")
        p("#endif")
        p("            }")
        p("        }")
        p("    }")
        p("#endif")
        p("    in = fopen(src, \"rb\");")
        p("    if (!in) return;")
        p("    out = fopen(dst, \"wb\");")
        p("    if (!out) { fclose(in); return; }")
        p("    for (;;) {")
        p("        n = fread(buf, 1, sizeof buf, in);")
        p("        if (n == 0) break;")
        p("        if (fwrite(buf, 1, n, out) != n) break;")
        p("    }")
        p("    fclose(in);")
        p("    fclose(out);")
        p("}")
        p("")
    if want_file_text_stream:
        p("/* System.IO.StreamWriter / StreamReader — FILE* text streams */")
        p("typedef FILE *StreamWriter;")
        p("typedef FILE *StreamReader;")
        p("")
        if want_file_create_text:
            p("/* System.IO.File.CreateText — fopen \"w\" (+ mkdir). */")
            p("static StreamWriter File_CreateText(const char *path) {")
            p("    FILE *fp;")
            p("#ifndef CRUST_NO_POSIX_MKDIR")
            p("    char dir[1024];")
            p("    int n, i;")
            p("    if (path && path[0]) {")
            p("        n = (int)strlen(path);")
            p("        if (n > 0 && (size_t)n < sizeof dir) {")
            p("            for (i = 0; i < n; i++) dir[i] = path[i];")
            p("            dir[n] = 0;")
            p("            for (i = n - 1; i >= 0; i--) {")
            p("#ifdef _WIN32")
            p("                if (dir[i] == '/' || dir[i] == '\\\\') {")
            p("                    dir[i] = 0;")
            p("                    if (dir[0]) _engine_mkdir_p(dir);")
            p("                    break;")
            p("                }")
            p("#else")
            p("                if (dir[i] == '/') {")
            p("                    dir[i] = 0;")
            p("                    if (dir[0]) _engine_mkdir_p(dir);")
            p("                    break;")
            p("                }")
            p("#endif")
            p("            }")
            p("        }")
            p("    }")
            p("#endif")
            p("    fp = fopen(path ? path : \"\", \"w\");")
            p("    return fp;")
            p("}")
            p("")
        if want_file_open_text:
            p("/* System.IO.File.OpenText — fopen \"r\". */")
            p("static StreamReader File_OpenText(const char *path) {")
            p("    if (!path || !path[0]) return 0;")
            p("    return fopen(path, \"r\");")
            p("}")
            p("")
        if want_file_create_text:
            p("static void StreamWriter_WriteLine(StreamWriter fp,")
            p("                                   const char *s) {")
            p("    if (!fp) return;")
            p("    fputs(s ? s : \"\", fp);")
            p("    fputc('\\n', fp);")
            p("    fflush(fp);")
            p("}")
            p("")
        if want_file_open_text:
            # Alternate two buffers so consecutive ReadLine() keep both lines.
            p("static char _engine_readline_buf[2][4096];")
            p("static int _engine_readline_i;")
            p("static const char *StreamReader_ReadLine(StreamReader fp) {")
            p("    char *buf;")
            p("    size_t n;")
            p("    if (!fp) return \"\";")
            p("    buf = _engine_readline_buf[_engine_readline_i++ & 1];")
            p("    if (!fgets(buf, (int)sizeof _engine_readline_buf[0], fp))")
            p("        return \"\";")
            p("    n = strlen(buf);")
            p("    if (n > 0 && buf[n - 1] == '\\n') buf[n - 1] = 0;")
            p("    if (n > 1 && buf[n - 2] == '\\r') buf[n - 2] = 0;")
            p("    return buf;")
            p("}")
            p("")
        p("static void Stream_Close(FILE *fp) {")
        p("    if (fp) fclose(fp);")
        p("}")
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
        p("/* Call sites pick Console_WriteLine_{i,f,s} at rewrite (no C11 generics). */")
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

    if want_go_tables:
        go_names = plan.get("go_names") or []
        go_comps = plan.get("go_components") or {}
        go_rb2d = plan.get("go_rigidbody2d") or {}
        go_rb3d = plan.get("go_rigidbody") or {}
        p("/* GameObject.Find / GetComponent — authored scene tables only */")
        p("static const int _engine_go_count = %d;" % len(go_names))
        if go_names:
            p("static const char *_engine_go_name[%d] = {" % len(go_names))
            for n in go_names:
                p("    %s," % _c_string(n))
            p("};")
        else:
            p("static const char *_engine_go_name[1] = { \"\" };")
        # Unity catches script exceptions: log + unwind the current method.
        p("static jmp_buf _engine_script_jmp;")
        p("static int _engine_in_script = 0;")
        p("static void _engine_null_reference_at(")
        p("    const char *cls, const char *method,")
        p("    const char *path, int line) {")
        p("    fprintf(stderr, \"NullReferenceException: Object reference "
          "not set to an instance of an object\\n\");")
        p("    if (cls && method && path && path[0] && line > 0)")
        p("        fprintf(stderr, \"%s.%s () (at %s:%d)\\n\",")
        p("                cls, method, path, line);")
        p("    else if (cls && method)")
        p("        fprintf(stderr, \"%s.%s ()\\n\", cls, method);")
        p("    if (_engine_in_script)")
        p("        longjmp(_engine_script_jmp, 1);")
        p("}")
        p("")
        if want_add_any:
            p("static void _engine_cant_add_component("
              "const char *comp, int go) {")
            p("    const char *gon;")
            p("    if (go < 0 || go >= _engine_go_count) gon = \"\";")
            p("    else gon = _engine_go_name[go];")
            p("    fprintf(stderr, \"Can't add component '%s' to %s "
              "because such a component is already added to the "
              "game object!\\n\",")
            p("            comp, gon);")
            p("}")
            p("")
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
            mb_budget = int(add_budget.get(cname) or 0)
            if mb_budget:
                p("static int _engine_go_%s[%d] = { %s };" % (
                    idn, len(vals), ", ".join(vals)))
            else:
                p("static const int _engine_go_%s[%d] = { %s };" % (
                    idn, len(vals), ", ".join(vals)))
            # this instance i → GO index (for GetComponent on this).
            authored_n = int(plan["classes"][cname]["n"])
            cap_n = authored_n + mb_budget
            rev = ["-1"] * max(1, cap_n)
            for n, cmap in go_comps.items():
                if cname in cmap and n in go_names:
                    gi = go_names.index(n)
                    rev[cmap[cname]] = str(gi)
            if mb_budget:
                p("static int _engine_%s_go_of[%d] = { %s };" % (
                    idn, len(rev), ", ".join(rev)))
            else:
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
        # Authored uGUI GetComponent<T> — opaque GO handles (not AddComponent invent).
        ui_gc = sorted(getcomponent_types & _UI_GETCOMPONENT_TYPES)
        if ui_gc and want_go_tables:
            ui_maps = plan.get("go_ui_components") or {}
            go_n = max(1, len(go_names))
            for ty in ui_gc:
                idn = _c_ident(ty)
                present = set(ui_maps.get(ty) or [])
                vals = []
                for i, n in enumerate(go_names if go_names else [""]):
                    if ty == "RectTransform":
                        vals.append(str(i))
                    elif n in present:
                        vals.append(str(i))
                    else:
                        vals.append("-1")
                p("/* GetComponent<%s> — authored UI; handle is GO index */" % ty)
                p("static const int _engine_go_%s[%d] = { %s };" % (
                    idn, go_n, ", ".join(vals)))
                p("static int GameObject_GetComponent_%s(int go) {" % idn)
                p("    if (go < 0 || go >= _engine_go_count) return -1;")
                p("    return _engine_go_%s[go];" % idn)
                p("}")
                p("")
        # Emit GetComponent_<T> for every packed class (and requested types).
        for cname in sorted(set(plan["classes"]) | (
                getcomponent_types - _PHYSICS_COMPONENTS
                - _UI_GETCOMPONENT_TYPES) | (
                add_types - _ADDABLE_BUILTINS)):
            if cname not in plan["classes"]:
                continue
            idn = _c_ident(cname)
            mb_budget = int(add_budget.get(cname) or 0)
            p("static int GameObject_GetComponent_%s(int go) {" % idn)
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_%s[go];" % idn)
            p("}")
            p("")
            if mb_budget:
                cap = int(plan["classes"][cname]["n"]) + mb_budget
                p("static int GameObject_AddComponent_%s(int go) {" % idn)
                p("    int ex;")
                p("    if (go < 0 || go >= _engine_go_count) return -1;")
                p("    ex = _engine_go_%s[go];" % idn)
                if cname in disallow_multi:
                    p("    if (ex >= 0) {")
                    p("        _engine_cant_add_component(\"%s\", go);"
                      % cname)
                    p("        return -1;")
                    p("    }")
                else:
                    p("    if (ex >= 0) return ex;")
                p("    if (_%s_inst_count >= %d) return -1;" % (idn, cap))
                p("    ex = _%s_inst_count;" % idn)
                p("    _%s_inst_count = _%s_inst_count + 1;" % (idn, idn))
                p("    _engine_go_%s[go] = ex;" % idn)
                p("    if (ex >= 0 && ex < %d)" % cap)
                p("        _engine_%s_go_of[ex] = go;" % idn)
                p("    return ex;")
                p("}")
                p("")
                p("static char _%s_tostring_buf[256];" % idn)
                p("static const char *%s_ToString(int ci) {" % idn)
                p("    int n, go;")
                p("    if (ci < 0 || ci >= _%s_inst_count) return \"null\";"
                  % idn)
                p("    go = _engine_%s_go_of[ci];" % idn)
                p("    if (go < 0 || go >= _engine_go_count) return \"null\";")
                p("    n = snprintf(_%s_tostring_buf, sizeof _%s_tostring_buf,"
                  % (idn, idn))
                p("                 \"%%s (%s)\", _engine_go_name[go]);" % cname)
                p("    if (n < 0 || (size_t)n >= sizeof _%s_tostring_buf)"
                  % idn)
                p("        return _engine_go_name[go];")
                p("    return _%s_tostring_buf;" % idn)
                p("}")
                p("")
        if want_rb2d:
            vals = []
            for n in go_names:
                vals.append(str(go_rb2d[n]) if n in go_rb2d else "-1")
            if not vals:
                vals = ["-1"]
            rb2d_add = int(add_budget.get("Rigidbody2D") or 0)
            if rb2d_add:
                p("static int _engine_go_Rigidbody2D[%d] = { %s };" % (
                    len(vals), ", ".join(vals)))
            else:
                p("static const int _engine_go_Rigidbody2D[%d] = { %s };" % (
                    len(vals), ", ".join(vals)))
            p("static int GameObject_GetComponent_Rigidbody2D(int go) {")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_Rigidbody2D[go];")
            p("}")
            p("")
            if rb2d_add:
                p("static int GameObject_AddComponent_Rigidbody2D(int go) {")
                p("    int ex, oi, oc, c;")
                p("    if (go < 0 || go >= _engine_go_count) return -1;")
                p("    ex = _engine_go_Rigidbody2D[go];")
                if "Rigidbody2D" in disallow_multi:
                    p("    if (ex >= 0) {")
                    p("        _engine_cant_add_component(\"Rigidbody2D\", go);")
                    p("        return -1;")
                    p("    }")
                else:
                    p("    if (ex >= 0) return ex;")
                p("    if (_Rigidbody2D_count >= %d) return -1;" % max(1, rb2d_cap))
                p("    ex = _Rigidbody2D_count;")
                p("    _Rigidbody2D_count = _Rigidbody2D_count + 1;")
                p("    _engine_go_Rigidbody2D[go] = ex;")
                p("    _Rigidbody2D_vel_x[ex] = 0.f;")
                p("    _Rigidbody2D_vel_y[ex] = 0.f;")
                p("    _Rigidbody2D_gravity_scale[ex] = 1.f;")
                p("    _Rigidbody2D_linear_damping[ex] = 0.f;")
                p("    _Rigidbody2D_mass[ex] = 1.f;")
                p("    _Rigidbody2D_body_type[ex] = 0;")
                p("    oc = -1; oi = 0;")
                for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
                    idn = _c_ident(cname)
                    p("    c = _engine_go_%s[go];" % idn)
                    p("    if (c >= 0) { oc = %d; oi = c; }" % cid)
                p("    _Rigidbody2D_owner_class[ex] = oc;")
                p("    _Rigidbody2D_owner_inst[ex] = oi;")
                p("    return ex;")
                p("}")
                p("")
                p("static char _Rigidbody2D_tostring_buf[256];")
                p("static const char *Rigidbody2D_ToString(int ci) {")
                p("    int n, go;")
                p("    if (ci < 0 || ci >= _Rigidbody2D_count) return \"null\";")
                p("    for (go = 0; go < _engine_go_count; go = go + 1)")
                p("        if (_engine_go_Rigidbody2D[go] == ci) break;")
                p("    if (go >= _engine_go_count) return \"null\";")
                p("    n = snprintf(_Rigidbody2D_tostring_buf,")
                p("                 sizeof _Rigidbody2D_tostring_buf,")
                p("                 \"%s (UnityEngine.Rigidbody2D)\",")
                p("                 _engine_go_name[go]);")
                p("    if (n < 0 || (size_t)n >= sizeof _Rigidbody2D_tostring_buf)")
                p("        return _engine_go_name[go];")
                p("    return _Rigidbody2D_tostring_buf;")
                p("}")
                p("")
        if want_rb3d:
            vals = []
            for n in go_names:
                vals.append(str(go_rb3d[n]) if n in go_rb3d else "-1")
            if not vals:
                vals = ["-1"]
            rb3d_add = int(add_budget.get("Rigidbody") or 0)
            if rb3d_add:
                p("static int _engine_go_Rigidbody[%d] = { %s };" % (
                    len(vals), ", ".join(vals)))
            else:
                p("static const int _engine_go_Rigidbody[%d] = { %s };" % (
                    len(vals), ", ".join(vals)))
            p("static int GameObject_GetComponent_Rigidbody(int go) {")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_Rigidbody[go];")
            p("}")
            p("")
            if rb3d_add:
                p("static int GameObject_AddComponent_Rigidbody(int go) {")
                p("    int ex, oi, oc, c;")
                p("    if (go < 0 || go >= _engine_go_count) return -1;")
                p("    ex = _engine_go_Rigidbody[go];")
                if "Rigidbody" in disallow_multi:
                    p("    if (ex >= 0) {")
                    p("        _engine_cant_add_component(\"Rigidbody\", go);")
                    p("        return -1;")
                    p("    }")
                else:
                    p("    if (ex >= 0) return ex;")
                p("    if (_Rigidbody_count >= %d) return -1;" % max(1, rb3d_cap))
                p("    ex = _Rigidbody_count;")
                p("    _Rigidbody_count = _Rigidbody_count + 1;")
                p("    _engine_go_Rigidbody[go] = ex;")
                p("    _Rigidbody_vel_x[ex] = 0.f;")
                p("    _Rigidbody_vel_y[ex] = 0.f;")
                p("    _Rigidbody_vel_z[ex] = 0.f;")
                p("    _Rigidbody_mass[ex] = 1.f;")
                p("    _Rigidbody_drag[ex] = 0.f;")
                p("    _Rigidbody_use_gravity[ex] = 1;")
                p("    oc = -1; oi = 0;")
                for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
                    idn = _c_ident(cname)
                    p("    c = _engine_go_%s[go];" % idn)
                    p("    if (c >= 0) { oc = %d; oi = c; }" % cid)
                p("    _Rigidbody_owner_class[ex] = oc;")
                p("    _Rigidbody_owner_inst[ex] = oi;")
                p("    return ex;")
                p("}")
                p("")
                p("static char _Rigidbody_tostring_buf[256];")
                p("static const char *Rigidbody_ToString(int ci) {")
                p("    int n, go;")
                p("    if (ci < 0 || ci >= _Rigidbody_count) return \"null\";")
                p("    for (go = 0; go < _engine_go_count; go = go + 1)")
                p("        if (_engine_go_Rigidbody[go] == ci) break;")
                p("    if (go >= _engine_go_count) return \"null\";")
                p("    n = snprintf(_Rigidbody_tostring_buf,")
                p("                 sizeof _Rigidbody_tostring_buf,")
                p("                 \"%s (UnityEngine.Rigidbody)\",")
                p("                 _engine_go_name[go]);")
                p("    if (n < 0 || (size_t)n >= sizeof _Rigidbody_tostring_buf)")
                p("        return _engine_go_name[go];")
                p("    return _Rigidbody_tostring_buf;")
                p("}")
                p("")

        # Camera / Light / SpriteRenderer / Collider AddComponent pools.
        def _emit_simple_add(type_name, unity_name, budget_key=None,
                             authored_names=None):
            bk = budget_key or type_name
            bud = int(add_budget.get(bk) or 0)
            if type_name not in add_types and not bud:
                return
            if bud <= 0:
                bud = 1
            idn = _c_ident(type_name)
            go_n = max(1, len(go_names))
            authored_names = authored_names or set()
            init_vals = []
            for n in (go_names if go_names else [""]):
                # >=0 marks present (authored sentinel 0).
                init_vals.append("0" if n in authored_names else "-1")
            p("static int _engine_go_%s[%d] = { %s };" % (
                idn, go_n, ", ".join(init_vals)))
            p("static int _%s_live = 0;" % idn)
            p("static const int _%s_cap = %d;" % (idn, bud))
            p("static int _%s_owner_go[%d];" % (idn, bud))
            p("static int GameObject_GetComponent_%s(int go) {" % idn)
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_%s[go];" % idn)
            p("}")
            p("static int GameObject_AddComponent_%s(int go) {" % idn)
            p("    int ex;")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    ex = _engine_go_%s[go];" % idn)
            if type_name in disallow_multi:
                p("    if (ex >= 0) {")
                p("        _engine_cant_add_component(\"%s\", go);"
                  % type_name)
                p("        return -1;")
                p("    }")
            else:
                p("    if (ex >= 0) return ex;")
            p("    if (_%s_live >= _%s_cap) return -1;" % (idn, idn))
            p("    ex = _%s_live;" % idn)
            p("    _%s_live = _%s_live + 1;" % (idn, idn))
            p("    _engine_go_%s[go] = ex;" % idn)
            p("    _%s_owner_go[ex] = go;" % idn)
            p("    return ex;")
            p("}")
            p("static char _%s_tostring_buf[256];" % idn)
            p("static const char *%s_ToString(int ci) {" % idn)
            p("    int n, go;")
            p("    if (ci < 0 || ci >= _%s_live) return \"null\";" % idn)
            p("    go = _%s_owner_go[ci];" % idn)
            p("    if (go < 0 || go >= _engine_go_count) return \"null\";")
            p("    n = snprintf(_%s_tostring_buf, sizeof _%s_tostring_buf,"
              % (idn, idn))
            p("                 \"%%s (%s)\", _engine_go_name[go]);"
              % unity_name)
            p("    if (n < 0 || (size_t)n >= sizeof _%s_tostring_buf)" % idn)
            p("        return _engine_go_name[go];")
            p("    return _%s_tostring_buf;" % idn)
            p("}")
            p("")

        if want_add_camera:
            _emit_simple_add("Camera", "UnityEngine.Camera")
        if want_add_sprite:
            _emit_simple_add("SpriteRenderer", "UnityEngine.SpriteRenderer",
                             authored_names=go_has_sprite)
        if want_add_light:
            # Light also grows the authored light tables when present.
            bud = int(add_budget.get("Light") or 0) or 1
            go_n = max(1, len(go_names))
            p("static int _engine_go_Light[%d];" % go_n)
            p("static int _Light_go_inited = 0;")
            p("static void _Light_ensure_go_map(void) {")
            p("    int i;")
            p("    if (_Light_go_inited) return;")
            p("    _Light_go_inited = 1;")
            p("    for (i = 0; i < _engine_go_count; i = i + 1)")
            p("        _engine_go_Light[i] = -1;")
            p("}")
            p("static int GameObject_GetComponent_Light(int go) {")
            p("    _Light_ensure_go_map();")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_Light[go];")
            p("}")
            p("static int GameObject_AddComponent_Light(int go) {")
            p("    int ex;")
            p("    _Light_ensure_go_map();")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    ex = _engine_go_Light[go];")
            if "Light" in disallow_multi:
                p("    if (ex >= 0) {")
                p("        _engine_cant_add_component(\"Light\", go);")
                p("        return -1;")
                p("    }")
            else:
                p("    if (ex >= 0) return ex;")
            p("    if (_Light_count >= %d) return -1;" % max(1, light_cap))
            p("    ex = _Light_count;")
            p("    _Light_count = _Light_count + 1;")
            p("    _engine_go_Light[go] = ex;")
            p("    _Light_intensity[ex] = 1.f;")
            p("    _Light_color_r[ex] = 1.f;")
            p("    _Light_color_g[ex] = 1.f;")
            p("    _Light_color_b[ex] = 1.f;")
            p("    return ex;")
            p("}")
            p("static char _Light_tostring_buf[256];")
            p("static const char *Light_ToString(int ci) {")
            p("    int n, go;")
            p("    if (ci < 0 || ci >= _Light_count) return \"null\";")
            p("    for (go = 0; go < _engine_go_count; go = go + 1)")
            p("        if (_engine_go_Light[go] == ci) break;")
            p("    if (go >= _engine_go_count) return \"null\";")
            p("    n = snprintf(_Light_tostring_buf, sizeof _Light_tostring_buf,")
            p("                 \"%s (UnityEngine.Light)\", _engine_go_name[go]);")
            p("    if (n < 0 || (size_t)n >= sizeof _Light_tostring_buf)")
            p("        return _engine_go_name[go];")
            p("    return _Light_tostring_buf;")
            p("}")
            p("")
        if want_add_audio:
            asrc = plan.get("audiosources") or []
            as_budget = int(add_budget.get("AudioSource") or 0)
            as_cap = max(1, len(asrc) + as_budget)
            go_n = max(1, len(go_names))
            go_as = plan.get("go_audiosource") or {}
            vals = []
            for n in (go_names if go_names else [""]):
                vals.append(str(int(go_as[n])) if n in go_as else "-1")
            p("/* AudioSource — authored !u!82 + AddComponent pool (multi OK). */")
            p("extern int _AudioSource_count;")
            p("extern int _AudioSource_owner_go[%d];" % as_cap)
            p("extern int _AudioSource_play_on_awake[%d];" % as_cap)
            p("extern int _AudioSource_loop[%d];" % as_cap)
            p("extern int _AudioSource_mute[%d];" % as_cap)
            p("extern float _AudioSource_volume[%d];" % as_cap)
            p("extern float _AudioSource_pitch[%d];" % as_cap)
            p("extern int _AudioSource_clip[%d];" % as_cap)
            p("extern int _AudioSource_playing[%d];" % as_cap)
            p("static int _engine_go_AudioSource[%d] = { %s };" % (
                go_n, ", ".join(vals)))
            p("static int GameObject_GetComponent_AudioSource(int go) {")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    return _engine_go_AudioSource[go];")
            p("}")
            p("static int GameObject_AddComponent_AudioSource(int go) {")
            p("    int ex, first;")
            p("    if (go < 0 || go >= _engine_go_count) return -1;")
            p("    first = _engine_go_AudioSource[go];")
            p("    if (_AudioSource_count >= %d) return -1;" % as_cap)
            p("    ex = _AudioSource_count;")
            p("    _AudioSource_count = _AudioSource_count + 1;")
            p("    if (first < 0)")
            p("        _engine_go_AudioSource[go] = ex;")
            p("    _AudioSource_owner_go[ex] = go;")
            p("    _AudioSource_play_on_awake[ex] = 1;")
            p("    _AudioSource_loop[ex] = 0;")
            p("    _AudioSource_mute[ex] = 0;")
            p("    _AudioSource_volume[ex] = 1.f;")
            p("    _AudioSource_pitch[ex] = 1.f;")
            p("    _AudioSource_clip[ex] = -1;")
            p("    _AudioSource_playing[ex] = 0;")
            p("    return ex;")
            p("}")
            p("static void AudioSource_Play(int ci) {")
            p("    if (ci < 0 || ci >= _AudioSource_count) return;")
            p("    if (_AudioSource_mute[ci]) return;")
            p("    _AudioSource_playing[ci] = 1;")
            p("    /* host may observe _AudioSource_playing / clip */")
            p("}")
            p("static void AudioSource_Stop(int ci) {")
            p("    if (ci < 0 || ci >= _AudioSource_count) return;")
            p("    _AudioSource_playing[ci] = 0;")
            p("}")
            p("static char _AudioSource_tostring_buf[256];")
            p("static const char *AudioSource_ToString(int ci) {")
            p("    int n, go;")
            p("    if (ci < 0 || ci >= _AudioSource_count) return \"null\";")
            p("    go = _AudioSource_owner_go[ci];")
            p("    if (go < 0 || go >= _engine_go_count) return \"null\";")
            p("    n = snprintf(_AudioSource_tostring_buf,")
            p("                 sizeof _AudioSource_tostring_buf,")
            p("                 \"%s (UnityEngine.AudioSource)\",")
            p("                 _engine_go_name[go]);")
            p("    if (n < 0 || (size_t)n >= sizeof _AudioSource_tostring_buf)")
            p("        return _engine_go_name[go];")
            p("    return _AudioSource_tostring_buf;")
            p("}")
            p("")
        for col_ty, unity_ty in (
                ("BoxCollider2D", "UnityEngine.BoxCollider2D"),
                ("CircleCollider2D", "UnityEngine.CircleCollider2D"),
                ("BoxCollider", "UnityEngine.BoxCollider"),
                ("SphereCollider", "UnityEngine.SphereCollider")):
            if col_ty in add_types:
                _emit_simple_add(col_ty, unity_ty)

    if (want_ui or want_transform_find or want_transform_parent
            or want_set_parent or want_get_sibling):
        go_names = plan.get("go_names") or []
        go_n = max(1, len(go_names))
        go_parents = plan.get("go_parents") or ([-1] * go_n)
        if len(go_parents) < go_n:
            go_parents = list(go_parents) + [-1] * (go_n - len(go_parents))
        go_sib = plan.get("go_siblings") or (
            _build_go_sibling_indices(go_parents[:go_n]))
        if len(go_sib) < go_n:
            go_sib = list(go_sib) + [0] * (go_n - len(go_sib))
        want_go_parent_table = (
            want_ui or want_transform_find or want_transform_parent
            or want_set_parent)
        if want_go_parent_table:
            p("/* Transform hierarchy (live GO parents; seeded from m_Father) */"
              if (want_set_parent or want_transform_find) else
              "/* Authored Transform hierarchy (m_Father → GO index) */")
            # Mutable whenever Find or SetParent runs — Find walks live parents.
            if want_set_parent or want_transform_find:
                p("static int _engine_go_parent[%d] = { %s };" % (
                    go_n, ", ".join(str(int(x)) for x in go_parents[:go_n])))
            else:
                p("static const int _engine_go_parent[%d] = { %s };" % (
                    go_n, ", ".join(str(int(x)) for x in go_parents[:go_n])))
        if want_get_sibling:
            # Sibling order among children of the same parent (Unity).
            # Mutable when SetParent can change order; else authored seed.
            p("/* Live sibling indices (seeded from GO order under parent) */")
            if want_set_parent:
                p("static int _engine_go_sib[%d] = { %s };" % (
                    go_n, ", ".join(str(int(x)) for x in go_sib[:go_n])))
            else:
                p("static const int _engine_go_sib[%d] = { %s };" % (
                    go_n, ", ".join(str(int(x)) for x in go_sib[:go_n])))
            p("static int Transform_GetSiblingIndex(int go) {")
            p("    if (go < 0 || go >= %d) return 0;" % go_n)
            p("    return _engine_go_sib[go];")
            p("}")
            p("")
        if want_transform_parent:
            p("static int Transform_get_parent(int go) {")
            p("    if (go < 0 || go >= %d) return -1;" % go_n)
            p("    return _engine_go_parent[go];")
            p("}")
            p("")
        if want_transform_find:
            # Unity Transform.Find: direct child or path with '/'; -1 = null.
            # Walks the live parent table (updated by SetParent).
            p("static int Transform_Find(int parent, const char *path) {")
            p("    char seg[256];")
            p("    const char *p;")
            p("    int i, n, cur, found;")
            p("    if (parent < 0 || parent >= %d || !path || !path[0])"
              % go_n)
            p("        return -1;")
            p("    cur = parent;")
            p("    p = path;")
            p("    while (*p) {")
            p("        n = 0;")
            p("        while (*p && *p != '/' && n < 255) {")
            p("            seg[n] = *p;")
            p("            n = n + 1;")
            p("            p = p + 1;")
            p("        }")
            p("        seg[n] = 0;")
            p("        if (*p == '/') p = p + 1;")
            p("        if (n == 0) return -1;")
            p("        found = -1;")
            p("        for (i = 0; i < %d; i = i + 1) {" % go_n)
            p("            if (_engine_go_parent[i] == cur")
            p("                && strcmp(_engine_go_name[i], seg) == 0) {")
            p("                found = i;")
            p("                break;")
            p("            }")
            p("        }")
            p("        if (found < 0) return -1;")
            p("        cur = found;")
            p("    }")
            p("    return cur;")
            p("}")
            p("")
        if want_ui:
            p("/* GameObject.activeSelf — host pointer + authored Button */")
            p("static int _engine_go_active[%d];" % go_n)
            p("static int _engine_go_active_inited;")
            p("static int _engine_pointer_was_down;")
            p("static void _engine_go_active_init(void) {")
            p("    int i;")
            p("    if (_engine_go_active_inited) return;")
            p("    _engine_go_active_inited = 1;")
            p("    for (i = 0; i < %d; i = i + 1)" % go_n)
            p("        _engine_go_active[i] = 1;")
            p("}")
            p("static int _engine_go_active_in_hierarchy(int go) {")
            p("    int guard = 0;")
            p("    _engine_go_active_init();")
            p("    while (go >= 0 && go < %d && guard < %d) {"
              % (go_n, go_n + 2))
            p("        if (!_engine_go_active[go]) return 0;")
            p("        go = _engine_go_parent[go];")
            p("        guard = guard + 1;")
            p("    }")
            p("    return 1;")
            p("}")
            p("static void GameObject_SetActive(int go, int active) {")
            p("    _engine_go_active_init();")
            p("    if (go < 0 || go >= %d) return;" % go_n)
            p("    _engine_go_active[go] = active ? 1 : 0;")
            p("}")
            p("")
            p("static const int _engine_ui_button_count = %d;" % len(ui_buttons))
            if ui_buttons:
                nbtn = len(ui_buttons)

                def _f4(key):
                    return ", ".join(
                        "%sf" % repr(float(b[key][i]))
                        for b in ui_buttons for i in range(4))

                p("static const float _engine_ui_btn_ncx[%d] = { %s };" % (
                    nbtn, ", ".join("%sf" % repr(b["ncx"]) for b in ui_buttons)))
                p("static const float _engine_ui_btn_ncy[%d] = { %s };" % (
                    nbtn, ", ".join("%sf" % repr(b["ncy"]) for b in ui_buttons)))
                p("static const float _engine_ui_btn_nhw[%d] = { %s };" % (
                    nbtn, ", ".join("%sf" % repr(b["nhw"]) for b in ui_buttons)))
                p("static const float _engine_ui_btn_nhh[%d] = { %s };" % (
                    nbtn, ", ".join("%sf" % repr(b["nhh"]) for b in ui_buttons)))
                p("static const int _engine_ui_btn_go[%d] = { %s };" % (
                    nbtn, ", ".join(str(int(b["go"])) for b in ui_buttons)))
                p("static const int _engine_ui_btn_call_go[%d] = { %s };" % (
                    nbtn, ", ".join(str(int(b["calls"][0]["target_go"]))
                                    for b in ui_buttons)))
                p("static const int _engine_ui_btn_call_bool[%d] = { %s };" % (
                    nbtn, ", ".join(str(int(b["calls"][0]["bool_arg"]))
                                    for b in ui_buttons)))
                # ColorBlock (× multiplier) — Normal / Highlighted / Pressed / Disabled
                p("static const float _engine_ui_btn_col_n[%d] = { %s };" % (
                    nbtn * 4, _f4("normal")))
                p("static const float _engine_ui_btn_col_h[%d] = { %s };" % (
                    nbtn * 4, _f4("highlighted")))
                p("static const float _engine_ui_btn_col_p[%d] = { %s };" % (
                    nbtn * 4, _f4("pressed")))
                p("static const float _engine_ui_btn_col_d[%d] = { %s };" % (
                    nbtn * 4, _f4("disabled")))
                p("static float _engine_ui_btn_tint[%d];" % (nbtn * 4))
                p("static int _engine_ui_btn_tint_inited;")
                p("static void _engine_ui_btn_tint_init(void) {")
                p("    int i;")
                p("    if (_engine_ui_btn_tint_inited) return;")
                p("    _engine_ui_btn_tint_inited = 1;")
                p("    for (i = 0; i < %d; i = i + 1)" % (nbtn * 4))
                p("        _engine_ui_btn_tint[i] = _engine_ui_btn_col_n[i];")
                p("}")
            p("static void engine_ui_tick(void) {")
            p("    int pressed, i, hit;")
            p("    float px, py, sw, sh;")
            p("    _engine_go_active_init();")
            if ui_buttons:
                p("    _engine_ui_btn_tint_init();")
            p("    pressed = engine_pointer_down && !_engine_pointer_was_down;")
            p("    sw = (float)Screen_width;")
            p("    sh = (float)Screen_height;")
            p("    if (sw < 1.f) sw = 1.f;")
            p("    if (sh < 1.f) sh = 1.f;")
            p("    px = engine_pointer_x;")
            p("    py = engine_pointer_y;")
            p("    hit = -1;")
            if ui_buttons:
                p("    for (i = 0; i < _engine_ui_button_count; i = i + 1) {")
                p("        int go = _engine_ui_btn_go[i];")
                p("        float cx, cy, hw, hh, dx, dy;")
                p("        const float *col;")
                p("        if (go < 0 || go >= %d) continue;" % go_n)
                p("        if (!_engine_go_active_in_hierarchy(go)) {")
                p("            col = &_engine_ui_btn_col_d[i * 4];")
                p("            _engine_ui_btn_tint[i * 4 + 0] = col[0];")
                p("            _engine_ui_btn_tint[i * 4 + 1] = col[1];")
                p("            _engine_ui_btn_tint[i * 4 + 2] = col[2];")
                p("            _engine_ui_btn_tint[i * 4 + 3] = col[3];")
                p("            continue;")
                p("        }")
                p("        cx = _engine_ui_btn_ncx[i] * sw;")
                p("        cy = _engine_ui_btn_ncy[i] * sh;")
                p("        hw = _engine_ui_btn_nhw[i] * sw;")
                p("        hh = _engine_ui_btn_nhh[i] * sh;")
                p("        dx = px - cx; if (dx < 0.f) dx = -dx;")
                p("        dy = py - cy; if (dy < 0.f) dy = -dy;")
                p("        if (dx <= hw && dy <= hh) {")
                p("            if (hit < 0) hit = i;")
                p("            if (engine_pointer_down)")
                p("                col = &_engine_ui_btn_col_p[i * 4];")
                p("            else")
                p("                col = &_engine_ui_btn_col_h[i * 4];")
                p("        } else {")
                p("            col = &_engine_ui_btn_col_n[i * 4];")
                p("        }")
                p("        _engine_ui_btn_tint[i * 4 + 0] = col[0];")
                p("        _engine_ui_btn_tint[i * 4 + 1] = col[1];")
                p("        _engine_ui_btn_tint[i * 4 + 2] = col[2];")
                p("        _engine_ui_btn_tint[i * 4 + 3] = col[3];")
                p("    }")
                p("    if (pressed && hit >= 0) {")
                p("        GameObject_SetActive(_engine_ui_btn_call_go[hit],")
                p("                             _engine_ui_btn_call_bool[hit]);")
                p("    }")
            else:
                p("    (void)i; (void)hit; (void)px; (void)py;")
                p("    (void)sw; (void)sh; (void)pressed;")
            p("    _engine_pointer_was_down = engine_pointer_down;")
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

    if want_ctor_forbidden:
        p("/* Application.dataPath / persistentDataPath in field/.cctor. */")
        p("static void _engine_unity_ctor_forbidden(")
        p("    const char *api, const char *cls, const char *go_name,")
        p("    const char *path, int line) {")
        p("    fprintf(stderr,")
        p("        \"UnityException: get_%s is not allowed to be called from a \"")
        p("        \"MonoBehaviour constructor (or instance field initializer), \"")
        p("        \"call it in Awake or Start instead. Called from MonoBehaviour \"")
        p("        \"'%s' on game object '%s'.\\n\"")
        p("        \"See \\\"Script Serialization\\\" page in the Unity Manual for \"")
        p("        \"further details.\\n\"")
        p("        \"UnityEngine.Application.get_%s () \"")
        p("        \"(at <00000000000000000000000000000000>:0)\\n\"")
        p("        \"%s..cctor () (at %s:%d)\\n\"")
        p("        \"Rethrow as TypeInitializationException: The type \"")
        p("        \"initializer for '%s' threw an exception.\\n\",")
        p("        api, cls, go_name ? go_name : \"\", api, cls, path, line,")
        p("        cls);")
        p("}")
        p("")

    # Extensions.SetWorldScale → live localScale on the referenced Transform's GO.
    if plan.get("transform_field_targets"):
        live = set(plan.get("live_scale_classes") or [])
        p("/* Authored TransformExtensions.SetWorldScale (lossy≈parent∘local). */")
        p("static void _engine_set_world_scale(int tc, unsigned ti,")
        p("                                   float sx, float sy, float sz) {")
        p("    (void)sz;")
        p("    switch (tc) {")
        for cname in sorted(live):
            if cname not in class_ids:
                continue
            cid = class_ids[cname]
            idn = _c_ident(cname)
            p("    case %d:" % cid)
            p("        if (ti < (unsigned)_%s_inst_count) {" % idn)
            p("            _%s_scale_x[ti] = sx;" % idn)
            p("            _%s_scale_y[ti] = sy;" % idn)
            p("        }")
            p("        break;")
        p("    default: break;")
        p("    }")
        p("}")
        p("")

    if want_live_rot:
        p("/* Transform.Rotate(Space.Self): localRotation *= Euler(deg). */")
        p("static void _engine_transform_rotate_local(")
        p("    float *qx, float *qy, float *qz, float *qw,")
        p("    float *m00, float *m01, float *m10, float *m11,")
        p("    float ex_deg, float ey_deg, float ez_deg) {")
        p("    float hx = ex_deg * 0.008726646259971648f;")
        p("    float hy = ey_deg * 0.008726646259971648f;")
        p("    float hz = ez_deg * 0.008726646259971648f;")
        p("    float cx = cosf(hx); float sx = sinf(hx);")
        p("    float cy = cosf(hy); float sy = sinf(hy);")
        p("    float cz = cosf(hz); float sz = sinf(hz);")
        p("    float ex = sx * cy * cz + cx * sy * sz;")
        p("    float ey = cx * sy * cz - sx * cy * sz;")
        p("    float ez = cx * cy * sz - sx * sy * cz;")
        p("    float ew = cx * cy * cz + sx * sy * sz;")
        p("    float nx = (*qw) * ex + (*qx) * ew + (*qy) * ez - (*qz) * ey;")
        p("    float ny = (*qw) * ey - (*qx) * ez + (*qy) * ew + (*qz) * ex;")
        p("    float nz = (*qw) * ez + (*qx) * ey - (*qy) * ex + (*qz) * ew;")
        p("    float nw = (*qw) * ew - (*qx) * ex - (*qy) * ey - (*qz) * ez;")
        p("    float m = sqrtf(nx * nx + ny * ny + nz * nz + nw * nw);")
        p("    if (m > 1e-8f) {")
        p("        nx = nx / m; ny = ny / m; nz = nz / m; nw = nw / m;")
        p("    } else {")
        p("        nx = 0.f; ny = 0.f; nz = 0.f; nw = 1.f;")
        p("    }")
        p("    *qx = nx; *qy = ny; *qz = nz; *qw = nw;")
        p("    /* Ortho XY basis: R * (lx,ly,0) — X/Y tilt foreshortens. */")
        p("    *m00 = 1.f - 2.f * (ny * ny + nz * nz);")
        p("    *m01 = 2.f * (nx * ny - nz * nw);")
        p("    *m10 = 2.f * (nx * ny + nz * nw);")
        p("    *m11 = 1.f - 2.f * (nx * nx + nz * nz);")
        p("}")
        p("")
        p("/* Quaternion.LookRotation(forward, up) → local quat + XY basis. */")
        p("static void _engine_quat_look_rotation(")
        p("    float *qx, float *qy, float *qz, float *qw,")
        p("    float *m00, float *m01, float *m10, float *m11,")
        p("    float dx, float dy, float dz,")
        p("    float ux, float uy, float uz) {")
        p("    float len = sqrtf(dx * dx + dy * dy + dz * dz);")
        p("    float rx, ry, rz, rlen;")
        p("    float m00r, m01r, m02r, m10r, m11r, m12r, m20r, m21r, m22r;")
        p("    float trace, s, nx, ny, nz, nw;")
        p("    float uxi = ux, uyi = uy, uzi = uz;")
        p("    if (len < 1e-8f) {")
        p("        *qx = 0.f; *qy = 0.f; *qz = 0.f; *qw = 1.f;")
        p("        *m00 = 1.f; *m01 = 0.f; *m10 = 0.f; *m11 = 1.f;")
        p("        return;")
        p("    }")
        p("    dx = dx / len; dy = dy / len; dz = dz / len;")
        p("    rx = uyi * dz - uzi * dy;")
        p("    ry = uzi * dx - uxi * dz;")
        p("    rz = uxi * dy - uyi * dx;")
        p("    rlen = sqrtf(rx * rx + ry * ry + rz * rz);")
        p("    if (rlen < 1e-6f) {")
        p("        uxi = 0.f; uyi = 0.f; uzi = 1.f;")
        p("        rx = uyi * dz - uzi * dy;")
        p("        ry = uzi * dx - uxi * dz;")
        p("        rz = uxi * dy - uyi * dx;")
        p("        rlen = sqrtf(rx * rx + ry * ry + rz * rz);")
        p("        if (rlen < 1e-8f) {")
        p("            *qx = 0.f; *qy = 0.f; *qz = 0.f; *qw = 1.f;")
        p("            *m00 = 1.f; *m01 = 0.f; *m10 = 0.f; *m11 = 1.f;")
        p("            return;")
        p("        }")
        p("    }")
        p("    rx = rx / rlen; ry = ry / rlen; rz = rz / rlen;")
        p("    ux = dy * rz - dz * ry;")
        p("    uy = dz * rx - dx * rz;")
        p("    uz = dx * ry - dy * rx;")
        p("    /* Columns = right, up, forward (Unity LookRotation). */")
        p("    m00r = rx; m01r = ux; m02r = dx;")
        p("    m10r = ry; m11r = uy; m12r = dy;")
        p("    m20r = rz; m21r = uz; m22r = dz;")
        p("    trace = m00r + m11r + m22r;")
        p("    if (trace > 0.f) {")
        p("        s = 0.5f / sqrtf(trace + 1.f);")
        p("        nw = 0.25f / s;")
        p("        nx = (m21r - m12r) * s;")
        p("        ny = (m02r - m20r) * s;")
        p("        nz = (m10r - m01r) * s;")
        p("    } else if (m00r > m11r && m00r > m22r) {")
        p("        s = 2.f * sqrtf(1.f + m00r - m11r - m22r);")
        p("        nw = (m21r - m12r) / s;")
        p("        nx = 0.25f * s;")
        p("        ny = (m01r + m10r) / s;")
        p("        nz = (m02r + m20r) / s;")
        p("    } else if (m11r > m22r) {")
        p("        s = 2.f * sqrtf(1.f + m11r - m00r - m22r);")
        p("        nw = (m02r - m20r) / s;")
        p("        nx = (m01r + m10r) / s;")
        p("        ny = 0.25f * s;")
        p("        nz = (m12r + m21r) / s;")
        p("    } else {")
        p("        s = 2.f * sqrtf(1.f + m22r - m00r - m11r);")
        p("        nw = (m10r - m01r) / s;")
        p("        nx = (m02r + m20r) / s;")
        p("        ny = (m12r + m21r) / s;")
        p("        nz = 0.25f * s;")
        p("    }")
        p("    *qx = nx; *qy = ny; *qz = nz; *qw = nw;")
        p("    *m00 = 1.f - 2.f * (ny * ny + nz * nz);")
        p("    *m01 = 2.f * (nx * ny - nz * nw);")
        p("    *m10 = 2.f * (nx * ny + nz * nw);")
        p("    *m11 = 1.f - 2.f * (nx * nx + nz * nz);")
        p("}")
        p("")
        p("/* Transform.LookAt: localRotation = LookRotation(to-from, up). */")
        p("static void _engine_transform_look_at(")
        p("    float *qx, float *qy, float *qz, float *qw,")
        p("    float *m00, float *m01, float *m10, float *m11,")
        p("    float fx, float fy, float fz,")
        p("    float tx, float ty, float tz) {")
        p("    float dx = tx - fx;")
        p("    float dy = ty - fy;")
        p("    float dz = tz - fz;")
        p("    _engine_quat_look_rotation(")
        p("        qx, qy, qz, qw, m00, m01, m10, m11,")
        p("        dx, dy, dz, 0.f, 1.f, 0.f);")
        p("}")
        p("")
        p("/* Transform.eulerAngles get: quat → degrees (Unity ZXY). */")
        p("static void _engine_quat_to_euler_deg(")
        p("    float qx, float qy, float qz, float qw,")
        p("    float *ex, float *ey, float *ez) {")
        p("    float sqx = qx * qx;")
        p("    float sqy = qy * qy;")
        p("    float sqz = qz * qz;")
        p("    float sqw = qw * qw;")
        p("    float unit = sqx + sqy + sqz + sqw;")
        p("    float test = qx * qy + qz * qw;")
        p("    float rad2deg = 57.29577951308232f;")
        p("    if (test > 0.499f * unit) {")
        p("        *ey = 2.f * atan2f(qx, qw) * rad2deg;")
        p("        *ex = 90.f;")
        p("        *ez = 0.f;")
        p("    } else if (test < -0.499f * unit) {")
        p("        *ey = -2.f * atan2f(qx, qw) * rad2deg;")
        p("        *ex = -90.f;")
        p("        *ez = 0.f;")
        p("    } else {")
        p("        *ey = atan2f(2.f * qy * qw - 2.f * qx * qz,")
        p("                    sqx - sqy - sqz + sqw) * rad2deg;")
        p("        *ex = asinf(2.f * test / unit) * rad2deg;")
        p("        *ez = atan2f(2.f * qx * qw - 2.f * qy * qz,")
        p("                    -sqx + sqy - sqz + sqw) * rad2deg;")
        p("    }")
        p("}")
        p("")
        p("/* Transform.eulerAngles set: localRotation = Euler(deg). */")
        p("static void _engine_transform_set_euler(")
        p("    float *qx, float *qy, float *qz, float *qw,")
        p("    float *m00, float *m01, float *m10, float *m11,")
        p("    float ex_deg, float ey_deg, float ez_deg) {")
        p("    float hx = ex_deg * 0.008726646259971648f;")
        p("    float hy = ey_deg * 0.008726646259971648f;")
        p("    float hz = ez_deg * 0.008726646259971648f;")
        p("    float cx = cosf(hx); float sx = sinf(hx);")
        p("    float cy = cosf(hy); float sy = sinf(hy);")
        p("    float cz = cosf(hz); float sz = sinf(hz);")
        p("    float nx = sx * cy * cz + cx * sy * sz;")
        p("    float ny = cx * sy * cz - sx * cy * sz;")
        p("    float nz = cx * cy * sz - sx * sy * cz;")
        p("    float nw = cx * cy * cz + sx * sy * sz;")
        p("    *qx = nx; *qy = ny; *qz = nz; *qw = nw;")
        p("    *m00 = 1.f - 2.f * (ny * ny + nz * nz);")
        p("    *m01 = 2.f * (nx * ny - nz * nw);")
        p("    *m10 = 2.f * (nx * ny + nz * nw);")
        p("    *m11 = 1.f - 2.f * (nx * nx + nz * nz);")
        p("}")
        p("")
        p("/* Transform.rotation set: localRotation = quat (unparented≈world). */")
        p("static void _engine_transform_set_quat(")
        p("    float *qx, float *qy, float *qz, float *qw,")
        p("    float *m00, float *m01, float *m10, float *m11,")
        p("    float nx, float ny, float nz, float nw) {")
        p("    float m = sqrtf(nx * nx + ny * ny + nz * nz + nw * nw);")
        p("    if (m > 1e-8f) {")
        p("        nx = nx / m; ny = ny / m; nz = nz / m; nw = nw / m;")
        p("    } else {")
        p("        nx = 0.f; ny = 0.f; nz = 0.f; nw = 1.f;")
        p("    }")
        p("    *qx = nx; *qy = ny; *qz = nz; *qw = nw;")
        p("    *m00 = 1.f - 2.f * (ny * ny + nz * nz);")
        p("    *m01 = 2.f * (nx * ny - nz * nw);")
        p("    *m10 = 2.f * (nx * ny + nz * nw);")
        p("    *m11 = 1.f - 2.f * (nx * nx + nz * nz);")
        p("}")
        p("")
        p("/* Quaternion.Slerp(a, b, t) — shortest-path spherical lerp. */")
        p("static void _engine_quat_slerp(")
        p("    float ax, float ay, float az, float aw,")
        p("    float bx, float by, float bz, float bw,")
        p("    float t,")
        p("    float *ox, float *oy, float *oz, float *ow) {")
        p("    float dot = ax * bx + ay * by + az * bz + aw * bw;")
        p("    float theta, st, wa, wb, nx, ny, nz, nw, m;")
        p("    if (t <= 0.f) {")
        p("        *ox = ax; *oy = ay; *oz = az; *ow = aw;")
        p("        return;")
        p("    }")
        p("    if (t >= 1.f) {")
        p("        *ox = bx; *oy = by; *oz = bz; *ow = bw;")
        p("        return;")
        p("    }")
        p("    if (dot < 0.f) {")
        p("        bx = -bx; by = -by; bz = -bz; bw = -bw;")
        p("        dot = -dot;")
        p("    }")
        p("    if (dot > 0.9995f) {")
        p("        nx = ax + t * (bx - ax);")
        p("        ny = ay + t * (by - ay);")
        p("        nz = az + t * (bz - az);")
        p("        nw = aw + t * (bw - aw);")
        p("    } else {")
        p("        if (dot > 1.f) dot = 1.f;")
        p("        theta = acosf(dot);")
        p("        st = sinf(theta);")
        p("        if (st < 1e-8f) {")
        p("            *ox = ax; *oy = ay; *oz = az; *ow = aw;")
        p("            return;")
        p("        }")
        p("        wa = sinf((1.f - t) * theta) / st;")
        p("        wb = sinf(t * theta) / st;")
        p("        nx = wa * ax + wb * bx;")
        p("        ny = wa * ay + wb * by;")
        p("        nz = wa * az + wb * bz;")
        p("        nw = wa * aw + wb * bw;")
        p("    }")
        p("    m = sqrtf(nx * nx + ny * ny + nz * nz + nw * nw);")
        p("    if (m > 1e-8f) {")
        p("        *ox = nx / m; *oy = ny / m; *oz = nz / m; *ow = nw / m;")
        p("    } else {")
        p("        *ox = 0.f; *oy = 0.f; *oz = 0.f; *ow = 1.f;")
        p("    }")
        p("}")
        p("")
        p("/* Quaternion.Inverse(q) — conjugate / |q|^2 (Unity). */")
        p("static void _engine_quat_inverse(")
        p("    float x, float y, float z, float w,")
        p("    float *ox, float *oy, float *oz, float *ow) {")
        p("    float n2 = x * x + y * y + z * z + w * w;")
        p("    float inv;")
        p("    if (n2 < 1e-20f) {")
        p("        *ox = 0.f; *oy = 0.f; *oz = 0.f; *ow = 1.f;")
        p("        return;")
        p("    }")
        p("    inv = 1.f / n2;")
        p("    *ox = -x * inv;")
        p("    *oy = -y * inv;")
        p("    *oz = -z * inv;")
        p("    *ow = w * inv;")
        p("}")
        p("")

    if want_live_rot or want_quat_angle:
        p("/* Quaternion.Angle(a, b) — degrees between rotations (Unity). */")
        p("static float _engine_quat_angle(")
        p("    float ax, float ay, float az, float aw,")
        p("    float bx, float by, float bz, float bw) {")
        p("    float dot = ax * bx + ay * by + az * bz + aw * bw;")
        p("    if (dot < 0.f) dot = -dot;")
        p("    if (dot > 1.f) dot = 1.f;")
        p("    return acosf(dot) * 114.59155902616465f;")
        p("}")
        p("")

    if want_live_rot:
        p("/* Quaternion.RotateTowards(from, to, maxDegreesDelta) — Unity. */")
        p("static void _engine_quat_rotate_towards(")
        p("    float ax, float ay, float az, float aw,")
        p("    float bx, float by, float bz, float bw,")
        p("    float max_deg,")
        p("    float *ox, float *oy, float *oz, float *ow) {")
        p("    float ang = _engine_quat_angle("
           "ax, ay, az, aw, bx, by, bz, bw);")
        p("    float t;")
        p("    if (ang < 1e-6f) {")
        p("        *ox = bx; *oy = by; *oz = bz; *ow = bw;")
        p("        return;")
        p("    }")
        p("    t = max_deg / ang;")
        p("    _engine_quat_slerp("
           "ax, ay, az, aw, bx, by, bz, bw, t, ox, oy, oz, ow);")
        p("}")
        p("")

    # Group methods by class; array comment sits on the group.
    methods_by = {}
    for a in analyses:
        for c in a["classes"]:
            methods_by.setdefault(c["name"], []).extend(
                [(c, m) for m in c["methods"]
                 if m["name"] not in ("Start",) or True])

    # MonoBehaviour OnCollision*2D(Collision2D) → dispatch after collide2d.
    collision2d_handlers = {}
    for cname, pairs in methods_by.items():
        msgs = {}
        for _c, m in pairs:
            if m["name"] not in _COLLISION2D_MSGS:
                continue
            arg = _collision2d_arg_name(m.get("args") or "")
            if not arg:
                continue
            msgs[m["name"]] = arg
        if msgs:
            collision2d_handlers[cname] = msgs
    want_collision2d_msgs = bool(collision2d_handlers) and want_col2d

    if want_collision2d_msgs:
        p("/* Collision2D.ToString — Unity object type name. */")
        p("static const char *Collision2D_ToString(int coll) {")
        p("    (void)coll;")
        p("    return \"UnityEngine.Collision2D\";")
        p("}")
        p("")

    tp_classes = set(plan.get("transform_point_classes") or [])
    if tp_classes:
        p("/* Transform.TransformPoint — defined after live TRS / world pos. */")
        for cname in sorted(tp_classes):
            if cname not in plan["classes"]:
                continue
            idn = _c_ident(cname)
            p("static float %s_TransformPoint_x(unsigned i,"
              " float lx, float ly, float lz);" % idn)
            p("static float %s_TransformPoint_y(unsigned i,"
              " float lx, float ly, float lz);" % idn)
            p("static float %s_TransformPoint_z(unsigned i,"
              " float lx, float ly, float lz);" % idn)
        p("")

    matrix_classes = set(plan.get("transform_matrix_classes") or [])
    if matrix_classes:
        p("/* Transform localToWorld / worldToLocal — after live TRS. */")
        for cname in sorted(matrix_classes):
            if cname not in plan["classes"]:
                continue
            idn = _c_ident(cname)
            p("static Matrix4x4 %s_localToWorldMatrix(unsigned i);" % idn)
            p("static Matrix4x4 %s_worldToLocalMatrix(unsigned i);" % idn)
        p("")

    if want_set_parent:
        p("/* Transform.SetParent — defined after live parent tables. */")
        p("static void Transform_SetParent(int child, int parent,")
        p("                               int world_stays);")
        p("")

    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        p("/* ---- %s group: instance array is defined in data.c ---- */" % idn)
        p("#define %s_AT(i) (_%s_inst_array[(i)])" % (idn, idn))
        p("")
        # Class-level const / static fields (FRAME_CNT, LOG_FILE_PATH, …).
        data_path = plan.get("data_path") or ""
        persistent_path = plan.get("persistent_data_path") or ""
        for f in cl.get("class_consts") or []:
            fname = f["name"]
            default = f.get("default")
            if f.get("ty") == "string":
                if isinstance(default, dict) and default.get("kind") == "dataPath+":
                    path = data_path + (default.get("suffix") or "")
                elif isinstance(default, dict) and default.get("kind") == "dataPath":
                    path = data_path
                elif (isinstance(default, dict)
                      and default.get("kind") == "persistentDataPath+"):
                    path = persistent_path + (default.get("suffix") or "")
                elif (isinstance(default, dict)
                      and default.get("kind") == "persistentDataPath"):
                    path = persistent_path
                elif isinstance(default, str):
                    path = default
                else:
                    path = ""
                p("static const char %s_%s[] = %s;" % (
                    idn, fname, _c_string(path)))
            elif isinstance(default, (int, float)) and default is not None:
                if f.get("ty") == "float":
                    p("static const float %s_%s = %sf;" % (
                        idn, fname, repr(float(default))))
                else:
                    p("static const int %s_%s = %d;" % (
                        idn, fname, int(default)))
            elif f.get("ty") in ("StreamWriter", "StreamReader"):
                p("static FILE *%s_%s;" % (idn, fname))
        if cl.get("class_consts"):
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
        emit_names = _reachable_emit_methods(
            [m for _c, m in methods_by.get(cname, [])])
        for c, m in methods_by.get(cname, []):
            if m["name"] in ("Awake", "OnEnable"):
                continue
            if m["name"] not in emit_names:
                continue
            # TypeInitializer failed — do not lower or run script methods.
            if cl.get("ctor_forbidden"):
                continue
            coll_param = None
            if m["name"] in _COLLISION2D_MSGS:
                coll_param = _collision2d_arg_name(m.get("args") or "")
                if not coll_param:
                    continue
            site = {
                "class": cname,
                "method": m["name"],
                "path": _assets_rel_path(c.get("path") or cl.get("path") or ""),
                "body_abs": int(m.get("body_abs") or 0),
                "file_text": c.get("file_text") or "",
            }
            body = _lower_method_body(
                m["body"], cl, plan, site=site,
                collision2d_param=coll_param)
            if coll_param:
                p("static void %s_%s(unsigned i, int %s) {"
                  % (idn, m["name"], coll_param))
            else:
                p("static void %s_%s(unsigned i) {" % (idn, m["name"]))
            for line in body.split("\n"):
                if line.strip():
                    p("    " + line.rstrip())
            p("}")
            p("")

        # Tick: Start once (Unity), then FixedUpdate / Update.
        # Script exceptions longjmp here — Unity continues the player loop.
        def _call_script(method, indent="            "):
            if want_go_tables:
                p(indent + "_engine_in_script = 1;")
                p(indent + "if (setjmp(_engine_script_jmp) == 0)")
                p(indent + "    %s_%s((unsigned)n);" % (idn, method))
                p(indent + "_engine_in_script = 0;")
            else:
                p(indent + "%s_%s((unsigned)n);" % (idn, method))

        has_start = any(m["name"] == "Start"
                        for _c, m in methods_by.get(cname, []))
        has_fixed = any(m["name"] == "FixedUpdate"
                        for _c, m in methods_by.get(cname, []))
        has_update = any(m["name"] == "Update"
                         for _c, m in methods_by.get(cname, []))
        ctor_forbidden = list(cl.get("ctor_forbidden") or [])
        if ctor_forbidden:
            # Unity TypeInitializationException — spam each frame, no script.
            fb = ctor_forbidden[0]
            api = fb.get("api") or "persistentDataPath"
            line = int(fb.get("line") or 0)
            spath = _assets_rel_path(
                cl.get("script_path") or cl.get("path") or "")
            p("void %s_FixedTick(void) { /* type init failed */ }" % idn)
            p("")
            p("void %s_Tick(void) {" % idn)
            p("    int n;")
            p("    for (n = 0; n < _%s_inst_count; n = n + 1) {" % idn)
            p("        const char *_gon = \"\";")
            if want_go_tables:
                p("        {")
                p("            int _dgo = _engine_go_of_%s((unsigned)n);" % idn)
                p("            if (_dgo >= 0 && _dgo < _engine_go_count)")
                p("                _gon = _engine_go_name[_dgo];")
                p("        }")
            else:
                # Fall back to authored instance name.
                for i, o in enumerate(cl.get("instances") or []):
                    if i == 0:
                        p("        if (n == 0) _gon = %s;"
                          % _c_string(o.get("name") or cname))
                    else:
                        p("        else if (n == %d) _gon = %s;"
                          % (i, _c_string(o.get("name") or cname)))
            p("        _engine_unity_ctor_forbidden(")
            p("            %s, %s, _gon, %s, %d);"
              % (_c_string(api), _c_string(cname), _c_string(spath), line))
            p("    }")
            p("}")
            p("")
            continue

        if has_start:
            p("static int _%s_started = 0;" % idn)
        p("void %s_FixedTick(void) {" % idn)
        if has_fixed:
            p("    int n;")
            p("    for (n = 0; n < _%s_inst_count; n = n + 1) {" % idn)
            _call_script("FixedUpdate", "        ")
            p("    }")
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
            p("        for (n = 0; n < _%s_inst_count; n = n + 1) {" % idn)
            _call_script("Start", "            ")
            p("        }")
            p("    }")
        if has_update:
            p("    for (n = 0; n < _%s_inst_count; n = n + 1) {" % idn)
            if want_destroy and plan.get("go_names"):
                p("        {")
                p("            int _dgo = _engine_go_of_%s((unsigned)n);" % idn)
                p("            if (_dgo >= 0 && _engine_go_destroyed[_dgo])")
                p("                continue;")
                p("        }")
            _call_script("Update", "        ")
            p("    }")
        elif not has_start:
            p("    /* no Update */")
        p("}")
        p("")

    # Live Transform hierarchy (m_Father): world = parent_world + local.
    # SetParent also needs these tables (mutable) even with no authored parents.
    want_set_parent = "transform.SetParent" in used_apis
    if plan.get("has_transform_parents") or want_set_parent:
        for cname, cl in sorted(plan["classes"].items()):
            idn = _c_ident(cname)
            if not _class_has_position(cl):
                continue
            n = max(1, cl["n"] + int(add_budget.get(cname) or 0))
            pcs = []
            pis = []
            for o in cl["instances"]:
                pcs.append(str(int(o.get("xf_parent_class_id", -1))))
                pis.append(str(int(o.get("xf_parent_inst") or 0)))
            for _pad in range(int(add_budget.get(cname) or 0)):
                pcs.append("-1")
                pis.append("0")
            while len(pcs) < n:
                pcs.append("-1")
                pis.append("0")
            if want_set_parent:
                p("static int _%s_xf_parent_class[%d] = { %s };"
                  % (idn, n, ", ".join(pcs)))
                p("static unsigned _%s_xf_parent_inst[%d] = { %s };"
                  % (idn, n, ", ".join(pis)))
            else:
                p("static const int _%s_xf_parent_class[%d] = { %s };"
                  % (idn, n, ", ".join(pcs)))
                p("static const unsigned _%s_xf_parent_inst[%d] = { %s };"
                  % (idn, n, ", ".join(pis)))
        p("")
        p("/* Live m_Father — world position follows parent at runtime. */")
        p("static void _engine_world_pos(int class_id, unsigned inst,")
        p("                             float *x, float *y, float *z,")
        p("                             int depth) {")
        p("    float lx = 0.f, ly = 0.f, lz = 0.f;")
        p("    int pc = -1;")
        p("    unsigned pi = 0u;")
        p("    if (depth > 64) { *x = 0.f; *y = 0.f; *z = 0.f; return; }")
        p("    switch (class_id) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl):
                continue
            p("    case %d:" % cid)
            p("        lx = %s_get_pos_x(inst);" % idn)
            p("        ly = %s_get_pos_y(inst);" % idn)
            if cl.get("two_d"):
                p("        lz = 0.f;")
            else:
                p("        lz = %s_get_pos_z(inst);" % idn)
            p("        pc = _%s_xf_parent_class[inst];" % idn)
            p("        pi = _%s_xf_parent_inst[inst];" % idn)
            p("        break;")
        p("    default:")
        p("        *x = 0.f; *y = 0.f; *z = 0.f;")
        p("        return;")
        p("    }")
        p("    if (pc < 0) { *x = lx; *y = ly; *z = lz; return; }")
        p("    {")
        p("        float px, py, pz;")
        p("        _engine_world_pos(pc, pi, &px, &py, &pz, depth + 1);")
        p("        *x = px + lx;")
        p("        *y = py + ly;")
        p("        *z = pz + lz;")
        p("    }")
        p("}")
        p("")

        if want_set_parent and want_go_tables:
            go_n = max(1, len(plan.get("go_names") or []))
            pos_classes = [
                (cname, class_ids[cname], _c_ident(cname))
                for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1])
                if _class_has_position(plan["classes"][cname])
            ]
            p("/* GO → packed (class, inst) for SetParent / world composition. */")
            p("static int _engine_go_xf(int go, int *oc, unsigned *oi) {")
            p("    int inst;")
            p("    if (go < 0 || go >= %d) return 0;" % go_n)
            for cname, cid, idn in pos_classes:
                p("    inst = _engine_go_%s[go];" % idn)
                p("    if (inst >= 0) { *oc = %d; *oi = (unsigned)inst; return 1; }"
                  % cid)
            p("    return 0;")
            p("}")
            p("static void _engine_set_xf_parent(int child_go, int parent_go) {")
            p("    int pc = -1;")
            p("    unsigned pi = 0u;")
            p("    int inst;")
            p("    if (parent_go >= 0)")
            p("        _engine_go_xf(parent_go, &pc, &pi);")
            for cname, cid, idn in pos_classes:
                p("    inst = _engine_go_%s[child_go];" % idn)
                p("    if (inst >= 0) {")
                p("        _%s_xf_parent_class[inst] = pc;" % idn)
                p("        _%s_xf_parent_inst[inst] = pi;" % idn)
                p("    }")
            p("}")
            p("static void _engine_set_local_pos_go(int go,")
            p("    float lx, float ly, float lz) {")
            p("    int inst;")
            for cname, cid, idn in pos_classes:
                cl = plan["classes"][cname]
                if cl.get("static"):
                    continue  # f16 pack — no setters
                p("    inst = _engine_go_%s[go];" % idn)
                p("    if (inst >= 0) {")
                p("        %s_set_pos_x((unsigned)inst, lx);" % idn)
                p("        %s_set_pos_y((unsigned)inst, ly);" % idn)
                if not cl.get("two_d"):
                    p("        %s_set_pos_z((unsigned)inst, lz);" % idn)
                p("    }")
            p("}")
            p("/* Transform.SetParent(parent, worldPositionStays=true). */")
            p("static void Transform_SetParent(int child, int parent,")
            p("                               int world_stays) {")
            p("    int guard = 0;")
            p("    int g;")
            p("    int cc = -1;")
            p("    unsigned ci = 0u;")
            p("    float wx = 0.f, wy = 0.f, wz = 0.f;")
            p("    float px = 0.f, py = 0.f, pz = 0.f;")
            if want_get_sibling:
                p("    int old_parent, old_sib, i, max_sib;")
            p("    if (child < 0 || child >= %d) return;" % go_n)
            p("    if (parent == child) return;")
            p("    if (parent >= %d) parent = -1;" % go_n)
            p("    g = parent;")
            p("    while (g >= 0 && guard < %d) {" % (go_n + 2))
            p("        if (g == child) return;")
            p("        g = _engine_go_parent[g];")
            p("        guard = guard + 1;")
            p("    }")
            p("    if (world_stays && _engine_go_xf(child, &cc, &ci))")
            p("        _engine_world_pos(cc, ci, &wx, &wy, &wz, 0);")
            if want_get_sibling:
                # Detach: close gap among old siblings; attach as last child.
                p("    old_parent = _engine_go_parent[child];")
                p("    old_sib = _engine_go_sib[child];")
                p("    for (i = 0; i < %d; i = i + 1) {" % go_n)
                p("        if (i == child) continue;")
                p("        if (_engine_go_parent[i] == old_parent")
                p("            && _engine_go_sib[i] > old_sib)")
                p("            _engine_go_sib[i] = _engine_go_sib[i] - 1;")
                p("    }")
            p("    _engine_go_parent[child] = parent;")
            if want_get_sibling:
                p("    max_sib = -1;")
                p("    for (i = 0; i < %d; i = i + 1) {" % go_n)
                p("        if (i == child) continue;")
                p("        if (_engine_go_parent[i] == parent")
                p("            && _engine_go_sib[i] > max_sib)")
                p("            max_sib = _engine_go_sib[i];")
                p("    }")
                p("    _engine_go_sib[child] = max_sib + 1;")
            p("    _engine_set_xf_parent(child, parent);")
            p("    if (world_stays && _engine_go_xf(child, &cc, &ci)) {")
            p("        if (parent >= 0) {")
            p("            int pc = -1;")
            p("            unsigned pi = 0u;")
            p("            if (_engine_go_xf(parent, &pc, &pi))")
            p("                _engine_world_pos(pc, pi, &px, &py, &pz, 0);")
            p("        }")
            p("        _engine_set_local_pos_go(child,")
            p("            wx - px, wy - py, wz - pz);")
            p("    }")
            p("}")
            p("")

    tp_classes = set(plan.get("transform_point_classes") or [])
    if tp_classes:
        # Unity Transform.TransformPoint: world = T + R * (S * local).
        # T is live world position; R/S are this transform's live local basis.
        # Parent chain translation matches _engine_world_pos (pack TRS subset).
        p("/* Transform.TransformPoint — live local→world (current TRS). */")
        p("static void _engine_transform_point(")
        p("    float wx, float wy, float wz,")
        p("    float m00, float m01, float m10, float m11,")
        p("    float sx, float sy,")
        p("    float lx, float ly, float lz,")
        p("    float *ox, float *oy, float *oz) {")
        p("    float px = lx * sx;")
        p("    float py = ly * sy;")
        p("    *ox = wx + m00 * px + m01 * py;")
        p("    *oy = wy + m10 * px + m11 * py;")
        p("    *oz = wz + lz;")
        p("}")
        p("")
        has_parents = bool(plan.get("has_transform_parents"))
        for cname in sorted(tp_classes):
            if cname not in plan["classes"]:
                continue
            cl = plan["classes"][cname]
            idn = _c_ident(cname)
            cid = class_ids[cname]
            p("static void %s_TransformPoint(unsigned i," % idn)
            p("    float lx, float ly, float lz,")
            p("    float *ox, float *oy, float *oz) {")
            p("    float wx, wy, wz;")
            if has_parents and _class_has_position(cl):
                p("    _engine_world_pos(%d, i, &wx, &wy, &wz, 0);" % cid)
            elif _class_has_position(cl):
                p("    wx = %s_get_pos_x(i);" % idn)
                p("    wy = %s_get_pos_y(i);" % idn)
                if cl.get("two_d"):
                    p("    wz = 0.f;")
                else:
                    p("    wz = %s_get_pos_z(i);" % idn)
            else:
                p("    wx = 0.f; wy = 0.f; wz = 0.f;")
            p("    _engine_transform_point(")
            p("        wx, wy, wz,")
            p("        _%s_rot_m00[i], _%s_rot_m01[i]," % (idn, idn))
            p("        _%s_rot_m10[i], _%s_rot_m11[i]," % (idn, idn))
            p("        _%s_scale_x[i], _%s_scale_y[i]," % (idn, idn))
            p("        lx, ly, lz, ox, oy, oz);")
            p("}")
            p("static float %s_TransformPoint_x(unsigned i," % idn)
            p("    float lx, float ly, float lz) {")
            p("    float ox, oy, oz;")
            p("    %s_TransformPoint(i, lx, ly, lz, &ox, &oy, &oz);" % idn)
            p("    return ox;")
            p("}")
            p("static float %s_TransformPoint_y(unsigned i," % idn)
            p("    float lx, float ly, float lz) {")
            p("    float ox, oy, oz;")
            p("    %s_TransformPoint(i, lx, ly, lz, &ox, &oy, &oz);" % idn)
            p("    return oy;")
            p("}")
            p("static float %s_TransformPoint_z(unsigned i," % idn)
            p("    float lx, float ly, float lz) {")
            p("    float ox, oy, oz;")
            p("    %s_TransformPoint(i, lx, ly, lz, &ox, &oy, &oz);" % idn)
            p("    return oz;")
            p("}")
            p("")

    matrix_classes = set(plan.get("transform_matrix_classes") or [])
    if matrix_classes:
        # Same affine subset as TransformPoint: T + R_xy * (S * p).
        p("/* Live localToWorld / worldToLocal from current pos / rot / scale. */")
        p("static Matrix4x4 _engine_matrix_identity(void) {")
        p("    Matrix4x4 m;")
        p("    m.m00 = 1.f; m.m01 = 0.f; m.m02 = 0.f; m.m03 = 0.f;")
        p("    m.m10 = 0.f; m.m11 = 1.f; m.m12 = 0.f; m.m13 = 0.f;")
        p("    m.m20 = 0.f; m.m21 = 0.f; m.m22 = 1.f; m.m23 = 0.f;")
        p("    m.m30 = 0.f; m.m31 = 0.f; m.m32 = 0.f; m.m33 = 1.f;")
        p("    return m;")
        p("}")
        p("static Matrix4x4 _engine_local_to_world_matrix(")
        p("    float wx, float wy, float wz,")
        p("    float r00, float r01, float r10, float r11,")
        p("    float sx, float sy) {")
        p("    Matrix4x4 m = _engine_matrix_identity();")
        p("    m.m00 = r00 * sx; m.m01 = r01 * sy; m.m03 = wx;")
        p("    m.m10 = r10 * sx; m.m11 = r11 * sy; m.m13 = wy;")
        p("    m.m23 = wz;")
        p("    return m;")
        p("}")
        p("static Matrix4x4 _engine_world_to_local_matrix(")
        p("    float wx, float wy, float wz,")
        p("    float r00, float r01, float r10, float r11,")
        p("    float sx, float sy) {")
        p("    Matrix4x4 m = _engine_matrix_identity();")
        p("    float a00 = r00 * sx; float a01 = r01 * sy;")
        p("    float a10 = r10 * sx; float a11 = r11 * sy;")
        p("    float det = a00 * a11 - a01 * a10;")
        p("    float inv00, inv01, inv10, inv11;")
        p("    if (det > -1e-12f && det < 1e-12f) {")
        p("        return m;")
        p("    }")
        p("    inv00 = a11 / det; inv01 = -a01 / det;")
        p("    inv10 = -a10 / det; inv11 = a00 / det;")
        p("    m.m00 = inv00; m.m01 = inv01;")
        p("    m.m10 = inv10; m.m11 = inv11;")
        p("    m.m03 = -(inv00 * wx + inv01 * wy);")
        p("    m.m13 = -(inv10 * wx + inv11 * wy);")
        p("    m.m23 = -wz;")
        p("    return m;")
        p("}")
        p("")
        has_parents = bool(plan.get("has_transform_parents"))
        for cname in sorted(matrix_classes):
            if cname not in plan["classes"]:
                continue
            cl = plan["classes"][cname]
            idn = _c_ident(cname)
            cid = class_ids[cname]

            def _emit_world_xyz():
                if has_parents and _class_has_position(cl):
                    p("    _engine_world_pos(%d, i, &wx, &wy, &wz, 0);" % cid)
                elif _class_has_position(cl):
                    p("    wx = %s_get_pos_x(i);" % idn)
                    p("    wy = %s_get_pos_y(i);" % idn)
                    if cl.get("two_d"):
                        p("    wz = 0.f;")
                    else:
                        p("    wz = %s_get_pos_z(i);" % idn)
                else:
                    p("    wx = 0.f; wy = 0.f; wz = 0.f;")

            p("static Matrix4x4 %s_localToWorldMatrix(unsigned i) {" % idn)
            p("    float wx, wy, wz;")
            _emit_world_xyz()
            p("    return _engine_local_to_world_matrix(")
            p("        wx, wy, wz,")
            p("        _%s_rot_m00[i], _%s_rot_m01[i]," % (idn, idn))
            p("        _%s_rot_m10[i], _%s_rot_m11[i]," % (idn, idn))
            p("        _%s_scale_x[i], _%s_scale_y[i]);" % (idn, idn))
            p("}")
            p("static Matrix4x4 %s_worldToLocalMatrix(unsigned i) {" % idn)
            p("    float wx, wy, wz;")
            _emit_world_xyz()
            p("    return _engine_world_to_local_matrix(")
            p("        wx, wy, wz,")
            p("        _%s_rot_m00[i], _%s_rot_m01[i]," % (idn, idn))
            p("        _%s_rot_m10[i], _%s_rot_m11[i]," % (idn, idn))
            p("        _%s_scale_x[i], _%s_scale_y[i]);" % (idn, idn))
            p("}")
            p("")

    if plan.get("camera_follows_parent"):
        cam = plan["camera"]
        lp = cam.get("local_pos") or (0.0, 0.0, 0.0)
        p("/* Main Camera m_Father — world follows packed parent each frame. */")
        p("static const float Camera_main_local_x = %sf;" % repr(float(lp[0])))
        p("static const float Camera_main_local_y = %sf;" % repr(float(lp[1])))
        p("static const float Camera_main_local_z = %sf;"
          % repr(float(lp[2]) if len(lp) > 2 else 0.0))
        p("static const int Camera_main_xf_parent_class = %d;"
          % int(cam.get("xf_parent_class_id", -1)))
        p("static const unsigned Camera_main_xf_parent_inst = %uu;"
          % int(cam.get("xf_parent_inst") or 0))
        p("static void _engine_sync_camera_main(void) {")
        p("    float px, py, pz;")
        p("    _engine_world_pos(Camera_main_xf_parent_class,")
        p("                     Camera_main_xf_parent_inst,")
        p("                     &px, &py, &pz, 0);")
        p("    Camera_main_pos_x = px + Camera_main_local_x;")
        p("    Camera_main_pos_y = py + Camera_main_local_y;")
        p("    Camera_main_pos_z = pz + Camera_main_local_z;")
        p("}")
        p("")

    if want_col2d or want_col3d:
        p("/* PhysicsMaterialCombine: Average=0 Multiply=1 Minimum=2 Maximum=3 */")
        p("static float _phys_mat_combine(float a, float b, int ca, int cb) {")
        p("    int mode = ca > cb ? ca : cb;")
        p("    if (mode > 3) mode = 0;")
        p("    if (mode == 1) return a * b;")
        p("    if (mode == 2) return a < b ? a : b;")
        p("    if (mode == 3) return a > b ? a : b;")
        p("    return 0.5f * (a + b);")
        p("}")
        p("")

    if want_col2d and col2d_list:
        p("/* Authored BoxCollider2D / CircleCollider2D — AABB contacts */")
        p("static void _col2d_center(int ci, float *out_x, float *out_y) {")
        p("    unsigned oi = (unsigned)_Collider2D_owner_inst[ci];")
        p("    float px = 0.f, py = 0.f;")
        p("    float c = _Collider2D_cos[ci], s = _Collider2D_sin[ci];")
        p("    float ox = _Collider2D_ox[ci], oy = _Collider2D_oy[ci];")
        p("    switch (_Collider2D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl):
                continue
            p("    case %d:" % cid)
            if plan.get("has_transform_parents"):
                p("        {")
                p("            float wx, wy, wz;")
                p("            _engine_world_pos(%d, oi, &wx, &wy, &wz, 0);"
                  % cid)
                p("            px = wx; py = wy;")
                p("        }")
            else:
                p("        px = %s_get_pos_x(oi);" % idn)
                p("        py = %s_get_pos_y(oi);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("    *out_x = px + c * ox - s * oy;")
        p("    *out_y = py + s * ox + c * oy;")
        p("}")
        p("")
        p("static void _col2d_set_pos(int ci, float nx, float ny) {")
        p("    unsigned oi = (unsigned)_Collider2D_owner_inst[ci];")
        p("    switch (_Collider2D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl) or cl.get("static"):
                continue
            p("    case %d:" % cid)
            p("        %s_set_pos_x(oi, nx);" % idn)
            p("        %s_set_pos_y(oi, ny);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("}")
        p("")
        p("static void _col2d_get_pos(int ci, float *ox, float *oy) {")
        p("    unsigned oi = (unsigned)_Collider2D_owner_inst[ci];")
        p("    *ox = 0.f; *oy = 0.f;")
        p("    switch (_Collider2D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl):
                continue
            p("    case %d:" % cid)
            if plan.get("has_transform_parents"):
                p("        {")
                p("            float wx, wy, wz;")
                p("            _engine_world_pos(%d, oi, &wx, &wy, &wz, 0);"
                  % cid)
                p("            *ox = wx; *oy = wy;")
                p("        }")
            else:
                p("        *ox = %s_get_pos_x(oi);" % idn)
                p("        *oy = %s_get_pos_y(oi);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("}")
        p("")
        if want_collision2d_msgs:
            nc = max(1, len(col2d_list))
            max_pairs = max(1, nc * (nc - 1) // 2)
            p("/* MonoBehaviour OnCollisionEnter/Stay/Exit2D after contacts */")
            p("static int _col2d_contact_a[%d];" % max_pairs)
            p("static int _col2d_contact_b[%d];" % max_pairs)
            p("static int _col2d_contact_n;")
            p("static int _col2d_prev_a[%d];" % max_pairs)
            p("static int _col2d_prev_b[%d];" % max_pairs)
            p("static int _col2d_prev_n;")
            p("")
            p("static void _col2d_add_contact(int a, int b) {")
            p("    int lo, hi, i;")
            p("    lo = (a < b) ? a : b;")
            p("    hi = (a < b) ? b : a;")
            p("    for (i = 0; i < _col2d_contact_n; i = i + 1)")
            p("        if (_col2d_contact_a[i] == lo && _col2d_contact_b[i] == hi)")
            p("            return;")
            p("    if (_col2d_contact_n >= %d) return;" % max_pairs)
            p("    _col2d_contact_a[_col2d_contact_n] = lo;")
            p("    _col2d_contact_b[_col2d_contact_n] = hi;")
            p("    _col2d_contact_n = _col2d_contact_n + 1;")
            p("}")
            p("")
            p("static int _col2d_pair_in(int lo, int hi,")
            p("    const int *pa, const int *pb, int n) {")
            p("    int i;")
            p("    for (i = 0; i < n; i = i + 1)")
            p("        if (pa[i] == lo && pb[i] == hi) return 1;")
            p("    return 0;")
            p("}")
            p("")
            p("static void _col2d_send_msg(int ci_self, int ci_other, int kind) {")
            p("    /* kind: 0 Enter, 1 Stay, 2 Exit */")
            p("    int oc = _Collider2D_owner_class[ci_self];")
            p("    unsigned oi = (unsigned)_Collider2D_owner_inst[ci_self];")
            p("    switch (oc) {")
            for cname in sorted(collision2d_handlers.keys()):
                cid = class_ids.get(cname)
                if cid is None:
                    continue
                idn = _c_ident(cname)
                msgs = collision2d_handlers[cname]
                p("    case %d:" % cid)
                for kind_i, msg in enumerate(
                        ("OnCollisionEnter2D",
                         "OnCollisionStay2D",
                         "OnCollisionExit2D")):
                    if msg not in msgs:
                        continue
                    p("        if (kind == %d) {" % kind_i)
                    if want_go_tables:
                        p("            _engine_in_script = 1;")
                        p("            if (setjmp(_engine_script_jmp) == 0)")
                        p("                %s_%s(oi, ci_other);"
                          % (idn, msg))
                        p("            _engine_in_script = 0;")
                    else:
                        p("            %s_%s(oi, ci_other);" % (idn, msg))
                    p("        }")
                p("        break;")
            p("    default: break;")
            p("    }")
            p("}")
            p("")
            p("static void engine_physics_collide2d_messages(void) {")
            p("    int i, lo, hi;")
            p("    for (i = 0; i < _col2d_contact_n; i = i + 1) {")
            p("        lo = _col2d_contact_a[i];")
            p("        hi = _col2d_contact_b[i];")
            p("        if (_col2d_pair_in(lo, hi, _col2d_prev_a, _col2d_prev_b,")
            p("                           _col2d_prev_n)) {")
            p("            _col2d_send_msg(lo, hi, 1);")
            p("            _col2d_send_msg(hi, lo, 1);")
            p("        } else {")
            p("            _col2d_send_msg(lo, hi, 0);")
            p("            _col2d_send_msg(hi, lo, 0);")
            p("        }")
            p("    }")
            p("    for (i = 0; i < _col2d_prev_n; i = i + 1) {")
            p("        lo = _col2d_prev_a[i];")
            p("        hi = _col2d_prev_b[i];")
            p("        if (!_col2d_pair_in(lo, hi, _col2d_contact_a,")
            p("                            _col2d_contact_b, _col2d_contact_n)) {")
            p("            _col2d_send_msg(lo, hi, 2);")
            p("            _col2d_send_msg(hi, lo, 2);")
            p("        }")
            p("    }")
            p("    _col2d_prev_n = _col2d_contact_n;")
            p("    for (i = 0; i < _col2d_contact_n; i = i + 1) {")
            p("        _col2d_prev_a[i] = _col2d_contact_a[i];")
            p("        _col2d_prev_b[i] = _col2d_contact_b[i];")
            p("    }")
            p("}")
            p("")
        p("static void engine_physics_collide2d(void) {")
        p("    int a, b;")
        if want_collision2d_msgs:
            p("    _col2d_contact_n = 0;")
        p("    /* Unordered pairs once. Dynamic-static moves only the dynamic.")
        p("     * Dynamic-dynamic splits by inverse mass; on a vertical MTV the")
        p("     * lower body is treated as immovable so stacks do not drive the")
        p("     * support through a static floor. */")
        p("    for (a = 0; a < _Collider2D_count; a = a + 1) {")
        p("        float ax, ay, bx, by, dx, dy, px, py, ahw, ahh, bhw, bhh;")
        p("        float c, s, sx, sy, nx, ny, fr, bn, sc;")
        p("        float inv_a, inv_b, inv_sum, wa, wb;")
        p("        int a_dyn, b_dyn, rb_a, rb_b;")
        p("        if (_Collider2D_is_trigger[a]) continue;")
        p("        a_dyn = (_Collider2D_body_type[a] == 0")
        p("                 && _Collider2D_rb2d[a] >= 0);")
        p("        for (b = a + 1; b < _Collider2D_count; b = b + 1) {")
        p("            float tx, ty, vx, vy, vn, vtx, vty;")
        p("            if (_Collider2D_is_trigger[b]) continue;")
        p("            b_dyn = (_Collider2D_body_type[b] == 0")
        p("                     && _Collider2D_rb2d[b] >= 0);")
        p("            if (!a_dyn && !b_dyn) continue;")
        p("            _col2d_center(a, &ax, &ay);")
        p("            c = _Collider2D_cos[a]; s = _Collider2D_sin[a];")
        p("            if (_Collider2D_kind[a] == 1) {")
        p("                ahw = _Collider2D_hw[a]; ahh = ahw;")
        p("            } else {")
        p("                ahw = fabsf(c) * _Collider2D_hw[a]")
        p("                    + fabsf(s) * _Collider2D_hh[a];")
        p("                ahh = fabsf(s) * _Collider2D_hw[a]")
        p("                    + fabsf(c) * _Collider2D_hh[a];")
        p("            }")
        p("            _col2d_center(b, &bx, &by);")
        p("            c = _Collider2D_cos[b]; s = _Collider2D_sin[b];")
        p("            if (_Collider2D_kind[b] == 1) {")
        p("                bhw = _Collider2D_hw[b]; bhh = bhw;")
        p("            } else {")
        p("                bhw = fabsf(c) * _Collider2D_hw[b]")
        p("                    + fabsf(s) * _Collider2D_hh[b];")
        p("                bhh = fabsf(s) * _Collider2D_hw[b]")
        p("                    + fabsf(c) * _Collider2D_hh[b];")
        p("            }")
        p("            dx = ax - bx; dy = ay - by;")
        p("            px = (ahw + bhw) - (dx < 0.f ? -dx : dx);")
        p("            py = (ahh + bhh) - (dy < 0.f ? -dy : dy);")
        p("            if (px <= 0.f || py <= 0.f) continue;")
        if want_collision2d_msgs:
            p("            _col2d_add_contact(a, b);")
        p("            sx = 0.f; sy = 0.f; nx = 0.f; ny = 0.f;")
        p("            if (px < py) {")
        p("                sx = (dx < 0.f) ? -px : px;")
        p("                nx = (sx < 0.f) ? -1.f : 1.f;")
        p("            } else {")
        p("                sy = (dy < 0.f) ? -py : py;")
        p("                ny = (sy < 0.f) ? -1.f : 1.f;")
        p("            }")
        p("            rb_a = _Collider2D_rb2d[a];")
        p("            rb_b = _Collider2D_rb2d[b];")
        p("            inv_a = 0.f;")
        p("            inv_b = 0.f;")
        p("            if (a_dyn) {")
        p("                inv_a = 1.f / _Rigidbody2D_mass[rb_a];")
        p("                if (inv_a < 0.f) inv_a = 0.f;")
        p("            }")
        p("            if (b_dyn) {")
        p("                inv_b = 1.f / _Rigidbody2D_mass[rb_b];")
        p("                if (inv_b < 0.f) inv_b = 0.f;")
        p("            }")
        p("            /* Vertical stack: lower dynamic is a support. */")
        p("            if (a_dyn && b_dyn && !(px < py)) {")
        p("                if (ay < by) inv_a = 0.f;")
        p("                else inv_b = 0.f;")
        p("            }")
        p("            inv_sum = inv_a + inv_b;")
        p("            if (inv_sum <= 1e-8f) continue;")
        p("            wa = inv_a / inv_sum;")
        p("            wb = inv_b / inv_sum;")
        p("            if (a_dyn && wa > 0.f) {")
        p("                _col2d_get_pos(a, &tx, &ty);")
        p("                _col2d_set_pos(a, tx + sx * wa, ty + sy * wa);")
        p("            }")
        p("            if (b_dyn && wb > 0.f) {")
        p("                _col2d_get_pos(b, &tx, &ty);")
        p("                _col2d_set_pos(b, tx - sx * wb, ty - sy * wb);")
        p("            }")
        p("            fr = _phys_mat_combine(")
        p("                _Collider2D_friction[a], _Collider2D_friction[b],")
        p("                _Collider2D_friction_combine[a],")
        p("                _Collider2D_friction_combine[b]);")
        p("            bn = _phys_mat_combine(")
        p("                _Collider2D_bounciness[a], _Collider2D_bounciness[b],")
        p("                _Collider2D_bounce_combine[a],")
        p("                _Collider2D_bounce_combine[b]);")
        p("            sc = 1.f - fr;")
        p("            if (sc < 0.f) sc = 0.f;")
        p("            if (sc > 1.f) sc = 1.f;")
        p("            if (a_dyn && wa > 0.f) {")
        p("                vx = _Rigidbody2D_vel_x[rb_a];")
        p("                vy = _Rigidbody2D_vel_y[rb_a];")
        p("                vn = vx * nx + vy * ny;")
        p("                vtx = vx - vn * nx;")
        p("                vty = vy - vn * ny;")
        p("                if (vn < 0.f) vn = -bn * vn;")
        p("                _Rigidbody2D_vel_x[rb_a] = vtx * sc + vn * nx;")
        p("                _Rigidbody2D_vel_y[rb_a] = vty * sc + vn * ny;")
        p("            }")
        p("            if (b_dyn && wb > 0.f) {")
        p("                /* Normal for b is opposite. */")
        p("                vx = _Rigidbody2D_vel_x[rb_b];")
        p("                vy = _Rigidbody2D_vel_y[rb_b];")
        p("                vn = vx * (-nx) + vy * (-ny);")
        p("                vtx = vx - vn * (-nx);")
        p("                vty = vy - vn * (-ny);")
        p("                if (vn < 0.f) vn = -bn * vn;")
        p("                _Rigidbody2D_vel_x[rb_b] = vtx * sc + vn * (-nx);")
        p("                _Rigidbody2D_vel_y[rb_b] = vty * sc + vn * (-ny);")
        p("            }")
        p("        }")
        p("    }")
        if want_collision2d_msgs:
            p("    engine_physics_collide2d_messages();")
        p("}")
        p("")

    if want_col3d and col3d_list:
        p("/* Authored BoxCollider / SphereCollider — AABB contacts */")
        p("static void _col3d_center(int ci, float *ox, float *oy, float *oz) {")
        p("    unsigned oi = (unsigned)_Collider3D_owner_inst[ci];")
        p("    float px = 0.f, py = 0.f, pz = 0.f;")
        p("    switch (_Collider3D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl):
                continue
            p("    case %d:" % cid)
            if plan.get("has_transform_parents"):
                p("        {")
                p("            float wx, wy, wz;")
                p("            _engine_world_pos(%d, oi, &wx, &wy, &wz, 0);"
                  % cid)
                p("            px = wx; py = wy; pz = wz;")
                p("        }")
            else:
                p("        px = %s_get_pos_x(oi);" % idn)
                p("        py = %s_get_pos_y(oi);" % idn)
                if not cl.get("two_d"):
                    p("        pz = %s_get_pos_z(oi);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("    *ox = px + _Collider3D_ox[ci];")
        p("    *oy = py + _Collider3D_oy[ci];")
        p("    *oz = pz + _Collider3D_oz[ci];")
        p("}")
        p("")
        p("static void _col3d_set_pos(int ci, float nx, float ny, float nz) {")
        p("    unsigned oi = (unsigned)_Collider3D_owner_inst[ci];")
        p("    switch (_Collider3D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl) or cl.get("static"):
                continue
            p("    case %d:" % cid)
            p("        %s_set_pos_x(oi, nx);" % idn)
            p("        %s_set_pos_y(oi, ny);" % idn)
            if not cl.get("two_d"):
                p("        %s_set_pos_z(oi, nz);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("}")
        p("")
        p("static void _col3d_get_pos(int ci, float *ox, float *oy, float *oz) {")
        p("    unsigned oi = (unsigned)_Collider3D_owner_inst[ci];")
        p("    *ox = 0.f; *oy = 0.f; *oz = 0.f;")
        p("    switch (_Collider3D_owner_class[ci]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl):
                continue
            p("    case %d:" % cid)
            if plan.get("has_transform_parents"):
                p("        _engine_world_pos(%d, oi, ox, oy, oz, 0);" % cid)
            else:
                p("        *ox = %s_get_pos_x(oi);" % idn)
                p("        *oy = %s_get_pos_y(oi);" % idn)
                if not cl.get("two_d"):
                    p("        *oz = %s_get_pos_z(oi);" % idn)
            p("        break;")
        p("    default: break;")
        p("    }")
        p("}")
        p("")
        p("static void engine_physics_collide3d(void) {")
        p("    int a, b;")
        p("    for (a = 0; a < _Collider3D_count; a = a + 1) {")
        p("        float ax, ay, az, bx, by, bz, dx, dy, dz;")
        p("        float px, py, pz, ahw, ahh, ahd, bhw, bhh, bhd;")
        p("        float sep, tx, ty, tz, nx, ny, nz;")
        p("        float vx, vy, vz, vn, vtx, vty, vtz, fr_d, fr_s, fr, bn, sc, vt_len;")
        p("        int rb;")
        p("        if (_Collider3D_body_type[a] != 0) continue;")
        p("        if (_Collider3D_is_trigger[a]) continue;")
        p("        rb = _Collider3D_rb3d[a];")
        p("        if (rb < 0) continue;")
        p("        _col3d_center(a, &ax, &ay, &az);")
        p("        if (_Collider3D_kind[a] == 1) {")
        p("            ahw = _Collider3D_hw[a]; ahh = ahw; ahd = ahw;")
        p("        } else {")
        p("            ahw = _Collider3D_hw[a];")
        p("            ahh = _Collider3D_hh[a];")
        p("            ahd = _Collider3D_hd[a];")
        p("        }")
        p("        for (b = 0; b < _Collider3D_count; b = b + 1) {")
        p("            if (a == b) continue;")
        p("            if (_Collider3D_is_trigger[b]) continue;")
        p("            _col3d_center(b, &bx, &by, &bz);")
        p("            if (_Collider3D_kind[b] == 1) {")
        p("                bhw = _Collider3D_hw[b]; bhh = bhw; bhd = bhw;")
        p("            } else {")
        p("                bhw = _Collider3D_hw[b];")
        p("                bhh = _Collider3D_hh[b];")
        p("                bhd = _Collider3D_hd[b];")
        p("            }")
        p("            dx = ax - bx; dy = ay - by; dz = az - bz;")
        p("            px = (ahw + bhw) - (dx < 0.f ? -dx : dx);")
        p("            py = (ahh + bhh) - (dy < 0.f ? -dy : dy);")
        p("            pz = (ahd + bhd) - (dz < 0.f ? -dz : dz);")
        p("            if (px <= 0.f || py <= 0.f || pz <= 0.f) continue;")
        p("            _col3d_get_pos(a, &tx, &ty, &tz);")
        p("            nx = 0.f; ny = 0.f; nz = 0.f;")
        p("            if (px <= py && px <= pz) {")
        p("                sep = (dx < 0.f) ? -px : px;")
        p("                tx = tx + sep;")
        p("                nx = (sep < 0.f) ? -1.f : 1.f;")
        p("            } else if (py <= px && py <= pz) {")
        p("                sep = (dy < 0.f) ? -py : py;")
        p("                ty = ty + sep;")
        p("                ny = (sep < 0.f) ? -1.f : 1.f;")
        p("            } else {")
        p("                sep = (dz < 0.f) ? -pz : pz;")
        p("                tz = tz + sep;")
        p("                nz = (sep < 0.f) ? -1.f : 1.f;")
        p("            }")
        p("            _col3d_set_pos(a, tx, ty, tz);")
        p("            fr_d = _phys_mat_combine(")
        p("                _Collider3D_dynamic_friction[a],")
        p("                _Collider3D_dynamic_friction[b],")
        p("                _Collider3D_friction_combine[a],")
        p("                _Collider3D_friction_combine[b]);")
        p("            fr_s = _phys_mat_combine(")
        p("                _Collider3D_static_friction[a],")
        p("                _Collider3D_static_friction[b],")
        p("                _Collider3D_friction_combine[a],")
        p("                _Collider3D_friction_combine[b]);")
        p("            bn = _phys_mat_combine(")
        p("                _Collider3D_bounciness[a], _Collider3D_bounciness[b],")
        p("                _Collider3D_bounce_combine[a],")
        p("                _Collider3D_bounce_combine[b]);")
        p("            vx = _Rigidbody_vel_x[rb];")
        p("            vy = _Rigidbody_vel_y[rb];")
        p("            vz = _Rigidbody_vel_z[rb];")
        p("            vn = vx * nx + vy * ny + vz * nz;")
        p("            vtx = vx - vn * nx;")
        p("            vty = vy - vn * ny;")
        p("            vtz = vz - vn * nz;")
        p("            vt_len = sqrtf(vtx * vtx + vty * vty + vtz * vtz);")
        p("            fr = (vt_len < 0.01f) ? fr_s : fr_d;")
        p("            if (vn < 0.f) vn = -bn * vn;")
        p("            sc = 1.f - fr;")
        p("            if (sc < 0.f) sc = 0.f;")
        p("            if (sc > 1.f) sc = 1.f;")
        p("            _Rigidbody_vel_x[rb] = vtx * sc + vn * nx;")
        p("            _Rigidbody_vel_y[rb] = vty * sc + vn * ny;")
        p("            _Rigidbody_vel_z[rb] = vtz * sc + vn * nz;")
        p("            _col3d_center(a, &ax, &ay, &az);")
        p("        }")
        p("    }")
        p("}")
        p("")

    if want_rb2d or want_rb3d:
        p("/* Authored Rigidbody / Rigidbody2D — gravity + integrate after FixedUpdate */")
        p("static void engine_physics_fixed(void) {")
        p("    int i;")
        if want_rb2d and rb2d_list:
            p("    for (i = 0; i < _Rigidbody2D_count; i = i + 1) {")
            p("        unsigned oi;")
            p("        float vx, vy;")
            p("        if (_Rigidbody2D_body_type[i] != 0) continue; /* Dynamic only */")
            p("        _Rigidbody2D_vel_x[i] = _Rigidbody2D_vel_x[i]")
            p("            + Physics2D_gravity_x * _Rigidbody2D_gravity_scale[i]")
            p("              * Time_fixedDeltaTime;")
            p("        _Rigidbody2D_vel_y[i] = _Rigidbody2D_vel_y[i]")
            p("            + Physics2D_gravity_y * _Rigidbody2D_gravity_scale[i]")
            p("              * Time_fixedDeltaTime;")
            # Box2D / Unity: v *= clamp(1 - damping * dt, 0, 1)
            p("        {")
            p("            float d = 1.f - _Rigidbody2D_linear_damping[i]")
            p("                * Time_fixedDeltaTime;")
            p("            if (d < 0.f) d = 0.f;")
            p("            if (d > 1.f) d = 1.f;")
            p("            _Rigidbody2D_vel_x[i] = _Rigidbody2D_vel_x[i] * d;")
            p("            _Rigidbody2D_vel_y[i] = _Rigidbody2D_vel_y[i] * d;")
            p("        }")
            p("        vx = _Rigidbody2D_vel_x[i] * Time_fixedDeltaTime;")
            p("        vy = _Rigidbody2D_vel_y[i] * Time_fixedDeltaTime;")
            p("        oi = (unsigned)_Rigidbody2D_owner_inst[i];")
            p("        switch (_Rigidbody2D_owner_class[i]) {")
            for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
                idn = _c_ident(cname)
                cl = plan["classes"][cname]
                if not _class_has_position(cl) or cl.get("static"):
                    continue
                p("        case %d:" % cid)
                p("            %s_set_pos_x(oi, %s_get_pos_x(oi) + vx);"
                  % (idn, idn))
                p("            %s_set_pos_y(oi, %s_get_pos_y(oi) + vy);"
                  % (idn, idn))
                p("            break;")
            p("        default: break;")
            p("        }")
            p("    }")
        if want_rb3d and rb3d_list:
            p("    for (i = 0; i < _Rigidbody_count; i = i + 1) {")
            p("        unsigned oi;")
            p("        float vx, vy, vz;")
            p("        if (_Rigidbody_use_gravity[i]) {")
            p("            _Rigidbody_vel_x[i] = _Rigidbody_vel_x[i]")
            p("                + Physics_gravity_x * Time_fixedDeltaTime;")
            p("            _Rigidbody_vel_y[i] = _Rigidbody_vel_y[i]")
            p("                + Physics_gravity_y * Time_fixedDeltaTime;")
            p("            _Rigidbody_vel_z[i] = _Rigidbody_vel_z[i]")
            p("                + Physics_gravity_z * Time_fixedDeltaTime;")
            p("        }")
            p("        {")
            p("            float d = 1.f - _Rigidbody_drag[i] * Time_fixedDeltaTime;")
            p("            if (d < 0.f) d = 0.f;")
            p("            if (d > 1.f) d = 1.f;")
            p("            _Rigidbody_vel_x[i] = _Rigidbody_vel_x[i] * d;")
            p("            _Rigidbody_vel_y[i] = _Rigidbody_vel_y[i] * d;")
            p("            _Rigidbody_vel_z[i] = _Rigidbody_vel_z[i] * d;")
            p("        }")
            p("        vx = _Rigidbody_vel_x[i] * Time_fixedDeltaTime;")
            p("        vy = _Rigidbody_vel_y[i] * Time_fixedDeltaTime;")
            p("        vz = _Rigidbody_vel_z[i] * Time_fixedDeltaTime;")
            p("        oi = (unsigned)_Rigidbody_owner_inst[i];")
            p("        switch (_Rigidbody_owner_class[i]) {")
            for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
                idn = _c_ident(cname)
                cl = plan["classes"][cname]
                if not _class_has_position(cl) or cl.get("static"):
                    continue
                p("        case %d:" % cid)
                p("            %s_set_pos_x(oi, %s_get_pos_x(oi) + vx);"
                  % (idn, idn))
                p("            %s_set_pos_y(oi, %s_get_pos_y(oi) + vy);"
                  % (idn, idn))
                if not cl.get("two_d"):
                    p("            %s_set_pos_z(oi, %s_get_pos_z(oi) + vz);"
                      % (idn, idn))
                p("            break;")
            p("        default: break;")
            p("        }")
            p("    }")
        if want_col2d and col2d_list:
            p("    engine_physics_collide2d();")
        if want_col3d and col3d_list:
            p("    engine_physics_collide3d();")
        p("}")
        p("")

    if want_anim and anim_players:
        p("/* Authored Animation / Animator — root pos + m_Sprite PPtr */")
        p("static float _anim_sample(const float *times, const float *vals,")
        p("                         int begin, int count, float t) {")
        p("    int i;")
        p("    float t0, t1, u;")
        p("    if (count <= 0) return 0.f;")
        p("    if (count == 1) return vals[begin];")
        p("    if (t <= times[begin]) return vals[begin];")
        p("    if (t >= times[begin + count - 1])")
        p("        return vals[begin + count - 1];")
        p("    for (i = 0; i < count - 1; i = i + 1) {")
        p("        t0 = times[begin + i];")
        p("        t1 = times[begin + i + 1];")
        p("        if (t >= t0 && t <= t1) {")
        p("            u = (t1 > t0) ? (t - t0) / (t1 - t0) : 0.f;")
        p("            return vals[begin + i]")
        p("                + (vals[begin + i + 1] - vals[begin + i]) * u;")
        p("        }")
        p("    }")
        p("    return vals[begin + count - 1];")
        p("}")
        p("")
        anim_skeys = anim_plan.get("sprite_keys") or []
        anim_sbinds = anim_plan.get("sprite_binds") or []
        if anim_skeys and anim_sbinds:
            p("/* Discrete PPtr hold (Unity SpriteRenderer.m_Sprite keys). */")
            p("static int _anim_sample_hold_i(const float *times,")
            p("                              const int *vals,")
            p("                              int begin, int count, float t) {")
            p("    int i;")
            p("    if (count <= 0) return 0;")
            p("    if (count == 1) return vals[begin];")
            p("    if (t <= times[begin]) return vals[begin];")
            p("    for (i = 0; i < count - 1; i = i + 1) {")
            p("        if (t >= times[begin + i]")
            p("            && t < times[begin + i + 1])")
            p("            return vals[begin + i];")
            p("    }")
            p("    return vals[begin + count - 1];")
            p("}")
            p("")
            p("static float _anim_sample_hold(const float *times,")
            p("                              const float *vals,")
            p("                              int begin, int count, float t) {")
            p("    int i;")
            p("    if (count <= 0) return 0.f;")
            p("    if (count == 1) return vals[begin];")
            p("    if (t <= times[begin]) return vals[begin];")
            p("    for (i = 0; i < count - 1; i = i + 1) {")
            p("        if (t >= times[begin + i]")
            p("            && t < times[begin + i + 1])")
            p("            return vals[begin + i];")
            p("    }")
            p("    return vals[begin + count - 1];")
            p("}")
            p("")
        p("static void engine_animation_tick(void) {")
        p("    int i;")
        p("    for (i = 0; i < _AnimPlayer_count; i = i + 1) {")
        p("        int ci, kb, kc;")
        p("        float t, len, nx, ny, nz;")
        p("        unsigned oi;")
        p("        if (!_AnimPlayer_playing[i]) continue;")
        p("        ci = _AnimPlayer_clip[i];")
        p("        len = _AnimClip_length[ci];")
        p("        if (len <= 0.f) continue;")
        p("        _AnimPlayer_time[i] = _AnimPlayer_time[i]")
        p("            + Time_deltaTime * _AnimPlayer_speed[i];")
        p("        t = _AnimPlayer_time[i];")
        p("        if (_AnimPlayer_loop[i]) {")
        p("            while (t >= len) t = t - len;")
        p("            while (t < 0.f) t = t + len;")
        p("            _AnimPlayer_time[i] = t;")
        p("        } else if (t > len) {")
        p("            t = len;")
        p("            _AnimPlayer_time[i] = t;")
        p("            _AnimPlayer_playing[i] = 0;")
        p("        }")
        p("        kb = _AnimClip_key_begin[ci];")
        p("        kc = _AnimClip_key_count[ci];")
        # Absolute localPosition only when the clip authors root PositionCurves.
        p("        if (kc > 0) {")
        p("            nx = _anim_sample(_AnimKey_t, _AnimKey_x, kb, kc, t);")
        p("            ny = _anim_sample(_AnimKey_t, _AnimKey_y, kb, kc, t);")
        p("            nz = _anim_sample(_AnimKey_t, _AnimKey_z, kb, kc, t);")
        p("            oi = (unsigned)_AnimPlayer_owner_inst[i];")
        p("            switch (_AnimPlayer_owner_class[i]) {")
        for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
            idn = _c_ident(cname)
            cl = plan["classes"][cname]
            if not _class_has_position(cl) or cl.get("static"):
                continue
            p("            case %d:" % cid)
            p("                %s_set_pos_x(oi, nx);" % idn)
            p("                %s_set_pos_y(oi, ny);" % idn)
            if not cl.get("two_d"):
                p("                %s_set_pos_z(oi, nz);" % idn)
            p("                break;")
        p("            default: break;")
        p("            }")
        p("        }")
        if anim_skeys and anim_sbinds:
            mutable = set(plan.get("sprite_draw_mutable") or [])
            p("        {")
            p("            int b0 = _AnimPlayer_sprite_bind_begin[i];")
            p("            int bc = _AnimPlayer_sprite_bind_count[i];")
            p("            int b;")
            p("            for (b = 0; b < bc; b = b + 1) {")
            p("                int bi = b0 + b;")
            p("                int skb = _AnimSpriteBind_key_begin[bi];")
            p("                int skc = _AnimSpriteBind_key_count[bi];")
            p("                int tex; float hw, hh;")
            p("                unsigned ti;")
            p("                if (skc <= 0) continue;")
            p("                tex = _anim_sample_hold_i(")
            p("                    _AnimSpriteKey_t, _AnimSpriteKey_tex,")
            p("                    skb, skc, t);")
            p("                hw = _anim_sample_hold(")
            p("                    _AnimSpriteKey_t, _AnimSpriteKey_hw,")
            p("                    skb, skc, t);")
            p("                hh = _anim_sample_hold(")
            p("                    _AnimSpriteKey_t, _AnimSpriteKey_hh,")
            p("                    skb, skc, t);")
            p("                ti = (unsigned)_AnimSpriteBind_target_inst[bi];")
            p("                switch (_AnimSpriteBind_target_class[bi]) {")
            for cname in sorted(mutable):
                if cname not in class_ids:
                    continue
                cid = class_ids[cname]
                idn = _c_ident(cname)
                p("                case %d:" % cid)
                p("                    _%s_draw_tex[ti] = tex;" % idn)
                p("                    _%s_draw_hw[ti] = hw;" % idn)
                p("                    _%s_draw_hh[ti] = hh;" % idn)
                p("                    break;")
            p("                default: break;")
            p("                }")
            p("            }")
            p("        }")
        p("    }")
        p("}")
        p("")

    p("void engine_tick(void) {")
    p("    /* Unity fixed clock: accumulate frame dt, step at fixedDeltaTime. */")
    p("    static float _engine_fixed_accum = 0.f;")
    p("    float _dt = Time_deltaTime;")
    p("    float _fixed_dt;")
    p("    int _fixed_guard;")
    p("    if (_dt > 0.33333334f) _dt = 0.33333334f; /* Time.maximumDeltaTime */")
    p("    if (_dt < 0.f) _dt = 0.f;")
    if "Time.time" in used_apis:
        p("    Time_time = Time_time + Time_deltaTime;")
    if want_ui:
        p("    engine_ui_tick();")
    if want_anim and anim_players:
        p("    engine_animation_tick();")
    p("    _engine_fixed_accum = _engine_fixed_accum + _dt;")
    p("    _fixed_dt = Time_fixedDeltaTime;")
    p("    if (_fixed_dt < 1e-8f) _fixed_dt = 0.02f;")
    p("    _fixed_guard = 0;")
    p("    while (_engine_fixed_accum >= _fixed_dt && _fixed_guard < 50) {")
    for cname in sorted(plan["classes"]):
        p("        %s_FixedTick();" % _c_ident(cname))
    if want_rb2d or want_rb3d:
        p("        engine_physics_fixed();")
    p("        _engine_fixed_accum = _engine_fixed_accum - _fixed_dt;")
    p("        _fixed_guard = _fixed_guard + 1;")
    p("    }")
    for cname in sorted(plan["classes"]):
        p("    %s_Tick();" % _c_ident(cname))
    if plan.get("camera_follows_parent"):
        p("    _engine_sync_camera_main();")
    p("}")
    p("")
    p("int engine_class_count(void) { return %d; }" % len(plan["classes"]))
    p("")

    # Draw list: authored SpriteRenderer + project PNG only.
    # Painter's order: TagManager sorting layer index, then m_SortingOrder.
    p("/* ---- draw list (SpriteRenderer + texture; see engine_draw.h) ---- */")
    p("typedef struct EngineDraw {")
    p("    float x, y, half_w, half_h;")
    p("    float m00, m01, m10, m11; /* local XY → world XY (full quat) */")
    p("    float r, g, b;")
    p("    float a; /* tint alpha (SpriteRenderer 1; Image m_Color.a) */")
    p("    int tex; /* index into engine_texture_*; -1 = none */")
    p("    int sorting_layer; /* TagManager m_SortingLayers index */")
    p("    int sorting_order; /* SpriteRenderer.m_SortingOrder */")
    p("} EngineDraw;")
    p("")
    if want_draw_sort:
        p("static int _engine_draw_cmp(const void *a, const void *b) {")
        p("    const EngineDraw *da = (const EngineDraw *)a;")
        p("    const EngineDraw *db = (const EngineDraw *)b;")
        p("    if (da->sorting_layer != db->sorting_layer)")
        p("        return da->sorting_layer - db->sorting_layer;")
        p("    return da->sorting_order - db->sorting_order;")
        p("}")
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
    if plan.get("camera_follows_parent"):
        p("    _engine_sync_camera_main();")
    any_sprite = False
    has_cam = bool(plan.get("camera"))
    mutable_spr = set(plan.get("sprite_draw_mutable") or [])
    go_names = plan.get("go_names") or []
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
        use_mut = cname in mutable_spr
        use_scale = cname in set(plan.get("live_scale_classes") or [])
        use_rot = cname in set(plan.get("live_rot_classes") or [])
        p("    { /* %s SpriteRenderer */" % idn)
        p("        static const float _spr_r[] = { %s };" % ", ".join(
            "%sf" % repr(float(sp["r"])) for _i, sp in spr_idx))
        p("        static const float _spr_g[] = { %s };" % ", ".join(
            "%sf" % repr(float(sp["g"])) for _i, sp in spr_idx))
        p("        static const float _spr_b[] = { %s };" % ", ".join(
            "%sf" % repr(float(sp["b"])) for _i, sp in spr_idx))
        p("        static const float _spr_a[] = { %s };" % ", ".join(
            "%sf" % repr(float(sp.get("a", 1.0))) for _i, sp in spr_idx))
        if not use_mut:
            p("        static const float _spr_hw[] = { %s };" % ", ".join(
                "%sf" % repr(float(sp["half_w"])) for _i, sp in spr_idx))
            p("        static const float _spr_hh[] = { %s };" % ", ".join(
                "%sf" % repr(float(sp["half_h"])) for _i, sp in spr_idx))
            p("        static const int _spr_tex[] = { %s };" % ", ".join(
                str(int(sp["tex_id"])) for _i, sp in spr_idx))
        if not use_rot:
            p("        static const float _spr_m00[] = { %s };" % ", ".join(
                "%sf" % repr(float(sp.get("m00", sp.get("cos_z", 1.0))))
                for _i, sp in spr_idx))
            p("        static const float _spr_m01[] = { %s };" % ", ".join(
                "%sf" % repr(float(sp.get("m01", -float(sp.get("sin_z", 0.0)))))
                for _i, sp in spr_idx))
            p("        static const float _spr_m10[] = { %s };" % ", ".join(
                "%sf" % repr(float(sp.get("m10", sp.get("sin_z", 0.0))))
                for _i, sp in spr_idx))
            p("        static const float _spr_m11[] = { %s };" % ", ".join(
                "%sf" % repr(float(sp.get("m11", sp.get("cos_z", 1.0))))
                for _i, sp in spr_idx))
        p("        static const int _spr_layer[] = { %s };" % ", ".join(
            str(int(sp.get("sorting_layer") or 0)) for _i, sp in spr_idx))
        p("        static const int _spr_order[] = { %s };" % ", ".join(
            str(int(sp.get("sorting_order") or 0)) for _i, sp in spr_idx))
        p("        static const unsigned _spr_i[] = { %s };" % ", ".join(
            str(i) for i, _sp in spr_idx))
        any_ui = any(sp.get("source") in ("ui", "ui_tmp")
                     for _i, sp in spr_idx)
        if any_ui:
            p("        static const int _spr_ui[] = { %s };" % ", ".join(
                "1" if sp.get("source") in ("ui", "ui_tmp") else "0"
                for _i, sp in spr_idx))
            p("        static const float _spr_ncx[] = { %s };" % ", ".join(
                "%sf" % repr(float(sp.get("ncx", 0.5))) for _i, sp in spr_idx))
            p("        static const float _spr_ncy[] = { %s };" % ", ".join(
                "%sf" % repr(float(sp.get("ncy", 0.5))) for _i, sp in spr_idx))
            p("        static const float _spr_nhw[] = { %s };" % ", ".join(
                "%sf" % repr(float(sp.get("nhw", 0.0))) for _i, sp in spr_idx))
            p("        static const float _spr_nhh[] = { %s };" % ", ".join(
                "%sf" % repr(float(sp.get("nhh", 0.0))) for _i, sp in spr_idx))
        if want_ui and go_names:
            go_vals = []
            btn_vals = []
            btn_by_go = {int(b["go"]): bi
                         for bi, b in enumerate(ui_buttons)}
            for i, _sp in spr_idx:
                n = cl["instances"][i].get("name") or "obj"
                gi = go_names.index(n) if n in go_names else -1
                go_vals.append(str(gi))
                btn_vals.append(str(btn_by_go.get(gi, -1)))
            p("        static const int _spr_go[] = { %s };" % ", ".join(go_vals))
            p("        static const int _spr_btn[] = { %s };" % ", ".join(
                btn_vals))
        p("        int k;")
        p("        for (k = 0; k < %d && n < max; k = k + 1) {" % len(spr_idx))
        p("            unsigned i = _spr_i[k];")
        if want_ui:
            p("            if (_spr_go[k] >= 0) {")
            p("                if (!_engine_go_active_in_hierarchy(_spr_go[k]))")
            p("                    continue;")
            p("            }")
        cid = class_ids[cname]

        def _emit_world_draw(ind):
            """World-space sprite path (non-UI), indented with ``ind``."""
            if plan.get("has_transform_parents"):
                p(ind + "float wx, wy, wz;")
                p(ind + "_engine_world_pos(%d, i, &wx, &wy, &wz, 0);" % cid)
                if has_cam:
                    p(ind + "{")
                    p(ind + "    float depth = wz - Camera_main_pos_z;")
                    p(ind + "    if (depth < Camera_main_nearClipPlane"
                      " || depth > Camera_main_farClipPlane)")
                    p(ind + "        continue;")
                    p(ind + "}")
                p(ind + "out[n].x = wx;")
                p(ind + "out[n].y = wy;")
            else:
                if has_cam:
                    if cl.get("two_d"):
                        p(ind + "float oz = 0.f;")
                    else:
                        p(ind + "float oz = %s_get_pos_z(i);" % idn)
                    p(ind + "{")
                    p(ind + "    float depth = oz - Camera_main_pos_z;")
                    p(ind + "    if (depth < Camera_main_nearClipPlane"
                      " || depth > Camera_main_farClipPlane)")
                    p(ind + "        continue;")
                    p(ind + "}")
                p(ind + "out[n].x = %s_get_pos_x(i);" % idn)
                p(ind + "out[n].y = %s_get_pos_y(i);" % idn)
            if use_mut:
                p(ind + "out[n].half_w = _%s_draw_hw[i];" % idn)
                p(ind + "out[n].half_h = _%s_draw_hh[i];" % idn)
                p(ind + "out[n].tex = _%s_draw_tex[i];" % idn)
            else:
                p(ind + "out[n].half_w = _spr_hw[k];")
                p(ind + "out[n].half_h = _spr_hh[k];")
                p(ind + "out[n].tex = _spr_tex[k];")
            if use_scale:
                p(ind + "out[n].half_w = out[n].half_w * _%s_scale_x[i];"
                  % idn)
                p(ind + "out[n].half_h = out[n].half_h * _%s_scale_y[i];"
                  % idn)

        if any_ui:
            p("            if (_spr_ui[k]) {")
            p("                float sw = (float)Screen_width;")
            p("                float sh = (float)Screen_height;")
            p("                float aspect, world_h, world_w;")
            p("                if (sw < 1.f) sw = 1.f;")
            p("                if (sh < 1.f) sh = 1.f;")
            p("                aspect = sw / sh;")
            p("                world_h = 2.f * Camera_main_orthographicSize;")
            p("                world_w = world_h * aspect;")
            p("                out[n].x = Camera_main_pos_x")
            p("                    + (_spr_ncx[k] - 0.5f) * world_w;")
            p("                out[n].y = Camera_main_pos_y")
            p("                    + (_spr_ncy[k] - 0.5f) * world_h;")
            p("                out[n].half_w = _spr_nhw[k] * world_w;")
            p("                out[n].half_h = _spr_nhh[k] * world_h;")
            if use_mut:
                p("                out[n].tex = _%s_draw_tex[i];" % idn)
            else:
                p("                out[n].tex = _spr_tex[k];")
            p("            } else {")
            _emit_world_draw("                ")
            p("            }")
        else:
            _emit_world_draw("            ")
        if use_rot:
            p("            out[n].m00 = _%s_rot_m00[i];" % idn)
            p("            out[n].m01 = _%s_rot_m01[i];" % idn)
            p("            out[n].m10 = _%s_rot_m10[i];" % idn)
            p("            out[n].m11 = _%s_rot_m11[i];" % idn)
        else:
            p("            out[n].m00 = _spr_m00[k];")
            p("            out[n].m01 = _spr_m01[k];")
            p("            out[n].m10 = _spr_m10[k];")
            p("            out[n].m11 = _spr_m11[k];")
        p("            out[n].r = _spr_r[k];")
        p("            out[n].g = _spr_g[k];")
        p("            out[n].b = _spr_b[k];")
        p("            out[n].a = _spr_a[k];")
        if want_ui:
            # ColorBlock multiplies Image.m_Color (Unity Selectable).
            p("            if (_spr_btn[k] >= 0) {")
            p("                int bi = _spr_btn[k] * 4;")
            p("                _engine_ui_btn_tint_init();")
            p("                out[n].r = out[n].r * _engine_ui_btn_tint[bi];")
            p("                out[n].g = out[n].g"
              " * _engine_ui_btn_tint[bi + 1];")
            p("                out[n].b = out[n].b"
              " * _engine_ui_btn_tint[bi + 2];")
            p("                out[n].a = out[n].a"
              " * _engine_ui_btn_tint[bi + 3];")
            p("            }")
        p("            out[n].sorting_layer = _spr_layer[k];")
        p("            out[n].sorting_order = _spr_order[k];")
        p("            n = n + 1;")
        p("        }")
        p("    }")
    if not any_sprite:
        p("    /* no authored SpriteRenderers — nothing to draw */")
    elif want_draw_sort:
        p("    if (n > 1)")
        p("        qsort(out, (size_t)n, sizeof(EngineDraw), _engine_draw_cmp);")
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
        p("        int need = _%s_inst_count * %d;" % (idn, dims))
        p("        if (n + need > max_floats) return n;")
        if cl.get("soa_dims"):
            # Contiguous float[N][dims] → one memcpy (libc / SIMD). Nested
            # element copies miss autovec for small N and lose big-N memcpy.
            p("        /* SoA: one contiguous table → memcpy (no gather) */")
            p("        if (need > 0)")
            p("            memcpy(dst + n, &_%s_pos[0][0]," % idn)
            p("                   (size_t)need * sizeof(float));")
        else:
            axes = ("pos_x", "pos_y", "pos_z")[:dims]
            kind_by = {m[0]: m[3] for m in cl["members"]}
            p("        int i;")
            p("        /* AoS gather (Unity-style); direct fields for autovec */")
            p("        for (i = 0; i < _%s_inst_count; i = i + 1) {" % idn)
            for axis_i, axis in enumerate(axes):
                if kind_by.get(axis) == "f32":
                    p("            dst[n + i * %d + %d] = %s_AT(i).%s;"
                      % (dims, axis_i, idn, axis))
                else:
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
        "    float m00, m01, m10, m11; /* local XY → world XY (full quat) */\n"
        "    float r, g, b;\n"
        "    float a; /* tint alpha */\n"
        "    int tex; /* engine_texture_* index; -1 if none */\n"
        "    int sorting_layer; /* TagManager m_SortingLayers index */\n"
        "    int sorting_order; /* SpriteRenderer.m_SortingOrder */\n"
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


def _rewrite_new_vector_assigns(text, idn, two_d=True):
    """`transform.position|localPosition =/+ = new Vector2/3(...)` / Vector3.zero."""

    def repl_eq(m):
        args = _split_call_args(m.group(1))
        if len(args) < 2:
            return m.group(0)
        zset = ""
        if (not two_d) and len(args) >= 3:
            zset = " %s_set_pos_z(i, (%s));" % (idn, args[2])
        return ("%s_set_pos_x(i, (%s)); %s_set_pos_y(i, (%s));%s" % (
            idn, args[0], idn, args[1], zset))

    def repl_add(m):
        args = _split_call_args(m.group(1))
        if len(args) < 2:
            return m.group(0)
        zadd = ""
        if (not two_d) and len(args) >= 3:
            zadd = (
                " %s_set_pos_z(i, %s_get_pos_z(i) + (%s));"
                % (idn, idn, args[2]))
        return (
            "%s_set_pos_x(i, %s_get_pos_x(i) + (%s)); "
            "%s_set_pos_y(i, %s_get_pos_y(i) + (%s));%s" % (
                idn, idn, args[0], idn, idn, args[1], zadd)
        )

    def repl_zero(m):
        if two_d:
            return "%s_set_pos_x(i, 0.f); %s_set_pos_y(i, 0.f);" % (idn, idn)
        return ("%s_set_pos_x(i, 0.f); %s_set_pos_y(i, 0.f); "
                "%s_set_pos_z(i, 0.f);" % (idn, idn, idn))

    flags = re.DOTALL
    for prop in ("position", "localPosition"):
        text = re.sub(
            r"transform\.%s\s*=\s*new\s+Vector2\s*\((.*?)\)\s*;" % prop,
            repl_eq, text, flags=flags)
        text = re.sub(
            r"transform\.%s\s*=\s*new\s+Vector3\s*\((.*?)\)\s*;" % prop,
            repl_eq, text, flags=flags)
        text = re.sub(
            r"transform\.%s\s*=\s*Vector3\.zero\s*;" % prop,
            repl_zero, text)
        text = re.sub(
            r"transform\.%s\s*\+=\s*new\s+Vector3\s*\((.*?)\)\s*;" % prop,
            repl_add, text, flags=flags)
    return text


def _rewrite_local_position_vec3_fields(text, cl):
    """Round-trip Vector3 fields ↔ live localPosition (packed pos tables)."""
    idn = _c_ident(cl["name"])
    has_z = not cl.get("two_d")
    for vf in cl.get("vec3_fields") or []:
        if has_z:
            load = (
                "%s_x = %s_get_pos_x(i);\n"
                "%s_y = %s_get_pos_y(i);\n"
                "%s_z = %s_get_pos_z(i);"
                % (vf, idn, vf, idn, vf, idn))
            store = (
                "%s_set_pos_x(i, %s_x);\n"
                "%s_set_pos_y(i, %s_y);\n"
                "%s_set_pos_z(i, %s_z);"
                % (idn, vf, idn, vf, idn, vf))
        else:
            load = (
                "%s_x = %s_get_pos_x(i);\n"
                "%s_y = %s_get_pos_y(i);\n"
                "%s_z = 0.f;"
                % (vf, idn, vf, idn, vf))
            store = (
                "%s_set_pos_x(i, %s_x);\n"
                "%s_set_pos_y(i, %s_y);"
                % (idn, vf, idn, vf))
        text = re.sub(
            r"(?<![_\w])%s\s*=\s*(?:this\s*\.\s*)?transform\s*\.\s*"
            r"localPosition\s*;" % vf,
            load, text)
        text = re.sub(
            r"(?:this\s*\.\s*)?transform\s*\.\s*localPosition\s*=\s*"
            r"(?<![_\w])%s\s*;" % vf,
            store, text)
    return text


def _rewrite_transform_matrices(text, cl, plan):
    """Lower transform.localToWorldMatrix / worldToLocalMatrix → live TRS."""
    if cl["name"] not in set(plan.get("transform_matrix_classes") or []):
        return text
    idn = _c_ident(cl["name"])
    text = re.sub(
        r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*localToWorldMatrix\b",
        "%s_localToWorldMatrix(i)" % idn, text)
    text = re.sub(
        r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*worldToLocalMatrix\b",
        "%s_worldToLocalMatrix(i)" % idn, text)
    return text


_VECTOR3_AXIS = {
    "right": (1.0, 0.0, 0.0),
    "left": (-1.0, 0.0, 0.0),
    "up": (0.0, 1.0, 0.0),
    "down": (0.0, -1.0, 0.0),
    "forward": (0.0, 0.0, 1.0),
    "back": (0.0, 0.0, -1.0),
}


def _parse_vector3_expr(a):
    """Parse `new Vector3(...)` or `Vector3.axis * expr` → (ex, ey, ez) C exprs."""
    a = a.strip()
    nm = re.match(r"new\s+Vector3\s*\((.*)\)$", a, flags=re.S)
    if nm:
        vargs = _split_call_args(nm.group(1))
        if len(vargs) >= 3:
            return vargs[0], vargs[1], vargs[2]
        return None
    am = re.match(
        r"Vector3\.(right|left|up|down|forward|back|zero|one)\s*$", a)
    if am:
        name = am.group(1)
        if name == "zero":
            return "0.f", "0.f", "0.f"
        if name == "one":
            return "1.f", "1.f", "1.f"
        ax, ay, az = _VECTOR3_AXIS[name]
        return ("%sf" % repr(float(ax)),
                "%sf" % repr(float(ay)),
                "%sf" % repr(float(az)))
    am = re.match(
        r"Vector3\.(right|left|up|down|forward|back)\s*\*\s*(.+)$",
        a, flags=re.S)
    if am:
        ax, ay, az = _VECTOR3_AXIS[am.group(1)]
        expr = am.group(2).strip()

        def _axis_comp(c):
            if c == 0.0:
                return "0.f"
            if c == 1.0:
                return "(%s)" % expr
            if c == -1.0:
                return "-(%s)" % expr
            return "(%s) * %sf" % (expr, repr(c))

        return (_axis_comp(ax), _axis_comp(ay), _axis_comp(az))
    return None


def _rewrite_transform_parent(text, cl, plan):
    """Lower transform.parent → Transform_get_parent(this_go)."""
    if not plan.get("go_names"):
        return text
    idn = _c_ident(cl["name"])
    go_expr = "_engine_go_of_%s(i)" % idn
    return re.sub(
        r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*parent\b",
        "Transform_get_parent(%s)" % go_expr,
        text)


def _setparent_go_expr(recv, cl):
    """Receiver of SetParent → child GO C expr (Transform ≡ GO index)."""
    idn = _c_ident(cl["name"])
    this_go = "_engine_go_of_%s(i)" % idn
    if not recv:
        return this_go
    recv = recv.strip()
    if recv in ("transform", "this"):
        return this_go
    for name, _ty, _bits, kind in cl.get("members") or []:
        if name != recv:
            continue
        if str(kind).startswith("idx:"):
            oty = kind.split(":", 1)[1]
            return "_engine_go_of_%s(%s)" % (_c_ident(oty), recv)
        break
    for f in cl.get("fields") or []:
        if f.get("name") != recv:
            continue
        ty = f.get("ty") or ""
        if ty in ("Transform", "GameObject"):
            return recv
        if ty and ty[0].isupper() and ty not in (
                "Vector2", "Vector3", "Quaternion", "string", "Color"):
            return "_engine_go_of_%s(%s)" % (_c_ident(ty), recv)
        break
    return recv


def _setparent_parent_expr(arg, cl):
    """SetParent first arg → parent GO C expr (-1 = null)."""
    a = arg.strip()
    if a == "null":
        return "-1"
    if re.match(r"(?:this\s*\.\s*)?transform\s*$", a):
        return "_engine_go_of_%s(i)" % _c_ident(cl["name"])
    # X.transform → GO of X
    m = re.match(r"(.+?)\s*\.\s*transform\s*$", a)
    if m:
        return _setparent_go_expr(m.group(1).strip(), cl)
    # Already Transform_get_parent(...) / GO index / field
    return a


def _rewrite_transform_set_parent(text, cl, plan):
    """Lower Transform.SetParent(parent[, worldStays]) → Transform_SetParent.

    Supports:
      transform.SetParent(null);
      transform.SetParent(null, false);
      transform.SetParent(other.transform, false);
      trs.SetParent(parent);
      cosmetic.transform.SetParent(graphicsTrs);
    Default worldPositionStays = true (Unity).
    """
    if not plan.get("go_names"):
        return text
    trs_locals = set()
    for lm in re.finditer(
            r"\b(?:Transform|GameObject)\s+(\w+)\b", text):
        trs_locals.add(lm.group(1))
    out = []
    i = 0
    # recv.transform.SetParent | (this.)transform.SetParent | recv.SetParent
    pat = re.compile(
        r"(?:(?<![.\w])(?P<tr>\w+)\s*\.\s*transform\s*\.\s*"
        r"|(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
        r"|(?<![.\w])(?P<trecv>\w+)\s*\.\s*)"
        r"SetParent\s*\(")
    while i < len(text):
        m = pat.search(text, i)
        if not m:
            out.append(text[i:])
            break
        # Bare Ident.SetParent only when Ident is a Transform/GameObject
        # field or local.
        if m.group("trecv") and not m.group("tr"):
            trecv = m.group("trecv")
            is_trs = trecv in trs_locals
            if not is_trs:
                for f in cl.get("fields") or []:
                    if f.get("name") == trecv and f.get("ty") in (
                            "Transform", "GameObject"):
                        is_trs = True
                        break
            if not is_trs:
                out.append(text[i:m.end()])
                i = m.end()
                continue
        open_paren = m.end() - 1
        parsed = _match_call_args(text, open_paren)
        if not parsed:
            out.append(text[i:open_paren + 1])
            i = open_paren + 1
            continue
        args_str, after = parsed
        args = _split_call_args(args_str)
        out.append(text[i:m.start()])
        if not args:
            out.append(text[m.start():after])
            i = after
            continue
        recv = m.group("tr") or m.group("trecv")
        child = _setparent_go_expr(recv, cl)
        parent = _setparent_parent_expr(args[0], cl)
        stays = "1"
        if len(args) >= 2:
            a1 = args[1].strip()
            if a1 in ("false", "False", "0"):
                stays = "0"
            elif a1 in ("true", "True", "1"):
                stays = "1"
            else:
                stays = "(%s) ? 1 : 0" % a1
        out.append("Transform_SetParent(%s, %s, %s)" % (child, parent, stays))
        i = after
    return "".join(out)


def _rewrite_transform_get_sibling_index(text, cl, plan):
    """Lower Transform.GetSiblingIndex() → Transform_GetSiblingIndex(go).

    Supports:
      transform.GetSiblingIndex();
      cosmetic.transform.GetSiblingIndex();
      trs.GetSiblingIndex();
    Reads the live sibling table (seeded authored; updated by SetParent).
    """
    if not plan.get("go_names"):
        return text
    trs_locals = set()
    for lm in re.finditer(
            r"\b(?:Transform|GameObject)\s+(\w+)\b", text):
        trs_locals.add(lm.group(1))
    out = []
    i = 0
    pat = re.compile(
        r"(?:(?<![.\w])(?P<tr>\w+)\s*\.\s*transform\s*\.\s*"
        r"|(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
        r"|(?<![.\w])(?P<trecv>\w+)\s*\.\s*)"
        r"GetSiblingIndex\s*\(")
    while i < len(text):
        m = pat.search(text, i)
        if not m:
            out.append(text[i:])
            break
        if m.group("trecv") and not m.group("tr"):
            trecv = m.group("trecv")
            is_trs = trecv in trs_locals
            if not is_trs:
                for f in cl.get("fields") or []:
                    if f.get("name") == trecv and f.get("ty") in (
                            "Transform", "GameObject"):
                        is_trs = True
                        break
            if not is_trs:
                out.append(text[i:m.end()])
                i = m.end()
                continue
        open_paren = m.end() - 1
        parsed = _match_call_args(text, open_paren)
        if not parsed:
            out.append(text[i:open_paren + 1])
            i = open_paren + 1
            continue
        _args_str, after = parsed
        out.append(text[i:m.start()])
        recv = m.group("tr") or m.group("trecv")
        go = _setparent_go_expr(recv, cl)
        out.append("Transform_GetSiblingIndex(%s)" % go)
        i = after
    return "".join(out)


def _rewrite_transform_game_object(text, cl, plan):
    """Lower transform.gameObject → this GO index (Transform ≡ GameObject)."""
    if not plan.get("go_names"):
        return text
    idn = _c_ident(cl["name"])
    go_expr = "_engine_go_of_%s(i)" % idn
    return re.sub(
        r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*gameObject\b",
        go_expr,
        text)


def _parse_transform_point_args(args_str, cl):
    """TransformPoint args → (lx, ly, lz) C exprs, or None."""
    args = _split_call_args(args_str)
    if len(args) == 3:
        return args[0], args[1], args[2]
    if len(args) != 1:
        return None
    a = args[0].strip()
    hit = _parse_vector3_expr(a)
    if hit:
        return hit
    # Field / property → packed _x/_y/_z (Offset → offset).
    name = a
    members = {n for n, _t, _b, _k in cl.get("members") or []}
    for cand in (name, name[:1].lower() + name[1:] if name else name):
        if not cand:
            continue
        if (cand + "_x") in members or cand in (cl.get("vec3_fields") or []):
            return (cand + "_x", cand + "_y",
                    cand + "_z" if (cand + "_z") in members else "0.f")
    return None


def _rewrite_transform_point(text, cl, plan):
    """Lower transform.TransformPoint → live local→world using current TRS.

    Supports:
      transform.TransformPoint(x, y, z).x/.y/.z
      transform.TransformPoint(new Vector3(...)).axis
      transform.TransformPoint(Offset).axis  # Vector3 field / property
    """
    if cl["name"] not in set(plan.get("transform_point_classes") or []):
        return text
    idn = _c_ident(cl["name"])
    out = []
    i = 0
    while i < len(text):
        m = re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*TransformPoint\s*\(",
            text[i:])
        if not m:
            out.append(text[i:])
            break
        start = i + m.start()
        open_paren = i + m.end() - 1
        parsed = _match_call_args(text, open_paren)
        if not parsed:
            out.append(text[i:open_paren + 1])
            i = open_paren + 1
            continue
        args_str, after = parsed
        comps = _parse_transform_point_args(args_str, cl)
        axis = None
        j = after
        am = re.match(r"\s*\.\s*([xyz])\b", text[j:])
        if am:
            axis = am.group(1)
            j = j + am.end()
        out.append(text[i:start])
        if comps is None:
            out.append(text[start:j])
        elif axis:
            lx, ly, lz = comps
            # After field rewrite, bare offset_x becomes Class_get_offset_x(i).
            # Emit getters when members exist so order is safe either way.
            def _comp(expr):
                mem = {n for n, _t, _b, _k in cl.get("members") or []}
                if expr in mem:
                    return "%s_get_%s(i)" % (idn, expr)
                return "(%s)" % expr
            out.append(
                "%s_TransformPoint_%s(i, %s, %s, %s)"
                % (idn, axis, _comp(lx), _comp(ly), _comp(lz)))
        else:
            # Bare Vector3 result — leave call for assign expand (rare).
            out.append(text[start:j])
        i = j
    return "".join(out)


def _rewrite_transform_find(text, cl, plan):
    """Lower Transform.Find(path) → Transform_Find(parent_go, path).

    Supports:
      transform.Find("Child");
      other.transform.Find("Child");
      trs.Find("Child");           # Transform / GameObject field or local
      GameObject.Find("P").transform.Find("C");
      GameObject_Find("P").Find("C");  # after .transform strip
    Unity: direct child name or nested path with '/'; missing → null (-1).
    Walks the live parent table (updated by SetParent).
    """
    if not plan.get("go_names"):
        return text
    # Locals declared as Transform / GameObject in this method body.
    trs_locals = set()
    for lm in re.finditer(
            r"\b(?:Transform|GameObject)\s+(\w+)\b", text):
        trs_locals.add(lm.group(1))
    out = []
    i = 0
    # GameObject.Find(...).transform.Find | GameObject_Find(...).Find |
    # recv.transform.Find | (this.)transform.Find | recv.Find
    pat = re.compile(
        r"(?:"
        r"(?P<gofind>(?:GameObject\s*\.\s*Find|GameObject_Find)\s*\([^)]*\))"
        r"(?:\s*\.\s*transform)?\s*\.\s*"
        r"|(?<![.\w])(?P<tr>\w+)\s*\.\s*transform\s*\.\s*"
        r"|(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
        r"|(?<![.\w])(?P<trecv>\w+)\s*\.\s*"
        r")"
        r"Find\s*\(")
    while i < len(text):
        m = pat.search(text, i)
        if not m:
            out.append(text[i:])
            break
        # Bare Ident.Find only when Ident is a Transform/GameObject field/local.
        if m.group("trecv") and not m.group("tr") and not m.group("gofind"):
            trecv = m.group("trecv")
            is_trs = trecv in trs_locals
            if not is_trs:
                for f in cl.get("fields") or []:
                    if f.get("name") == trecv and f.get("ty") in (
                            "Transform", "GameObject"):
                        is_trs = True
                        break
            if not is_trs:
                out.append(text[i:m.end()])
                i = m.end()
                continue
        open_paren = m.end() - 1
        parsed = _match_call_args(text, open_paren)
        if not parsed:
            out.append(text[i:open_paren + 1])
            i = open_paren + 1
            continue
        args_str, after = parsed
        out.append(text[i:m.start()])
        if m.group("gofind"):
            parent = m.group("gofind")
            parent = re.sub(
                r"GameObject\s*\.\s*Find\s*\(", "GameObject_Find(", parent)
        else:
            recv = m.group("tr") or m.group("trecv")
            parent = _setparent_go_expr(recv, cl)
        out.append("Transform_Find(%s, %s)" % (parent, args_str.strip()))
        i = after
    return "".join(out)


def _rewrite_transform_rotate(text, cl):
    """Lower transform.Rotate(...) → _engine_transform_rotate_local on packed quat.

    Supports:
      transform.Rotate(new Vector3(x, y, z));
      transform.Rotate(x, y, z);
      transform.Rotate(Vector3.right * expr);  # and up/forward/left/down/back
      optional trailing Space.Self / Space.World (World treated as Self for now)
    Degrees, local composition like Unity Space.Self.
    """
    idn = _c_ident(cl["name"])
    out = []
    i = 0
    while i < len(text):
        m = re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*Rotate\s*\(",
            text[i:])
        if not m:
            out.append(text[i:])
            break
        start = i + m.start()
        open_paren = i + m.end() - 1
        parsed = _match_call_args(text, open_paren)
        if not parsed:
            out.append(text[i:open_paren + 1])
            i = open_paren + 1
            continue
        args_str, after = parsed
        # Optional trailing semicolon.
        j = after
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j < len(text) and text[j] == ";":
            j += 1
        out.append(text[i:start])
        args = _split_call_args(args_str)
        ex = ey = ez = None
        if len(args) == 3 or (
                len(args) == 4
                and re.match(r"Space\.\w+", args[3].strip())):
            ex, ey, ez = args[0], args[1], args[2]
        elif len(args) == 1:
            hit = _parse_vector3_expr(args[0])
            if hit:
                ex, ey, ez = hit
        elif len(args) == 2 and re.match(r"Space\.\w+", args[1].strip()):
            hit = _parse_vector3_expr(args[0])
            if hit:
                ex, ey, ez = hit
        if ex is None:
            # Unsupported overload — keep source (should be rare).
            out.append(text[start:j])
        else:
            out.append(
                "_engine_transform_rotate_local("
                "&_%s_rot_x[i], &_%s_rot_y[i], &_%s_rot_z[i], &_%s_rot_w[i], "
                "&_%s_rot_m00[i], &_%s_rot_m01[i], &_%s_rot_m10[i], "
                "&_%s_rot_m11[i], "
                "(%s), (%s), (%s));"
                % (idn, idn, idn, idn, idn, idn, idn, idn, ex, ey, ez))
        i = j
    return "".join(out)


def _rewrite_transform_look_at(text, cl):
    """Lower transform.LookAt(...) → _engine_transform_look_at on packed quat.

    Supports:
      transform.LookAt(Camera.main.transform);
      transform.LookAt(Camera.main.transform.position);
      transform.LookAt(new Vector3(x, y, z));
      transform.LookAt(Vector3.zero / .one / .up / .forward / …);
    Optional trailing worldUp arg ignored (Unity default Vector3.up).
    """
    idn = _c_ident(cl["name"])
    from_x = "%s_get_pos_x(i)" % idn
    from_y = "%s_get_pos_y(i)" % idn
    if cl.get("two_d"):
        from_z = "0.f"
    else:
        from_z = "%s_get_pos_z(i)" % idn
    out = []
    i = 0
    while i < len(text):
        m = re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*LookAt\s*\(",
            text[i:])
        if not m:
            out.append(text[i:])
            break
        start = i + m.start()
        open_paren = i + m.end() - 1
        parsed = _match_call_args(text, open_paren)
        if not parsed:
            out.append(text[i:open_paren + 1])
            i = open_paren + 1
            continue
        args_str, after = parsed
        j = after
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j < len(text) and text[j] == ";":
            j += 1
        out.append(text[i:start])
        args = _split_call_args(args_str)
        tx = ty = tz = None
        if not args:
            out.append(text[start:j])
            i = j
            continue
        a0 = args[0].strip()
        if re.match(
                r"(?:UnityEngine\.)?Camera\.main\.transform"
                r"(?:\.position)?\s*$", a0):
            tx, ty, tz = ("Camera_main_pos_x", "Camera_main_pos_y",
                          "Camera_main_pos_z")
        else:
            nm = re.match(r"new\s+Vector3\s*\((.*)\)$", a0, flags=re.S)
            if nm:
                vargs = _split_call_args(nm.group(1))
                if len(vargs) >= 3:
                    tx, ty, tz = vargs[0], vargs[1], vargs[2]
            else:
                am = re.match(
                    r"Vector3\.(zero|one|right|left|up|down|forward|back)\s*$",
                    a0)
                if am:
                    name = am.group(1)
                    if name == "zero":
                        tx = ty = tz = "0.f"
                    elif name == "one":
                        tx = ty = tz = "1.f"
                    else:
                        ax, ay, az = _VECTOR3_AXIS[name]
                        tx = "%sf" % repr(float(ax))
                        ty = "%sf" % repr(float(ay))
                        tz = "%sf" % repr(float(az))
        if tx is None:
            out.append(text[start:j])
        else:
            out.append(
                "_engine_transform_look_at("
                "&_%s_rot_x[i], &_%s_rot_y[i], &_%s_rot_z[i], &_%s_rot_w[i], "
                "&_%s_rot_m00[i], &_%s_rot_m01[i], &_%s_rot_m10[i], "
                "&_%s_rot_m11[i], "
                "%s, %s, %s, (%s), (%s), (%s));"
                % (idn, idn, idn, idn, idn, idn, idn, idn,
                   from_x, from_y, from_z, tx, ty, tz))
        i = j
    return "".join(out)


def _rewrite_transform_euler_angles(text, cl):
    """Lower transform.eulerAngles = / += → get euler + set_euler on packed quat.

    Supports:
      transform.eulerAngles = new Vector3(...);
      transform.eulerAngles = Vector3.forward * expr;
      transform.eulerAngles += …;
    Degrees; matches Unity Quaternion.Euler / eulerAngles for unparented bodies.
    """
    idn = _c_ident(cl["name"])
    rot_args = (
        "&_%s_rot_x[i], &_%s_rot_y[i], &_%s_rot_z[i], &_%s_rot_w[i], "
        "&_%s_rot_m00[i], &_%s_rot_m01[i], &_%s_rot_m10[i], "
        "&_%s_rot_m11[i]" % ((idn,) * 8)
    )
    out = []
    i = 0
    while i < len(text):
        m = re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*eulerAngles\s*"
            r"(\+=|=)(?!=)",
            text[i:])
        if not m:
            out.append(text[i:])
            break
        start = i + m.start()
        op = m.group(1)
        rhs_start = i + m.end()
        # Scan RHS to terminating `;`.
        j = rhs_start
        depth = 0
        while j < len(text):
            c = text[j]
            if c == '"':
                j = _skip_c_string(text, j)
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == ";" and depth == 0:
                break
            j += 1
        rhs = text[rhs_start:j].strip()
        end = j + 1 if j < len(text) and text[j] == ";" else j
        out.append(text[i:start])
        hit = _parse_vector3_expr(rhs)
        if hit is None:
            out.append(text[start:end])
        else:
            ex, ey, ez = hit
            if op == "=":
                out.append(
                    "_engine_transform_set_euler(%s, (%s), (%s), (%s));"
                    % (rot_args, ex, ey, ez))
            else:
                out.append(
                    "{ float _eex, _eey, _eez; "
                    "_engine_quat_to_euler_deg("
                    "_%s_rot_x[i], _%s_rot_y[i], _%s_rot_z[i], _%s_rot_w[i], "
                    "&_eex, &_eey, &_eez); "
                    "_engine_transform_set_euler(%s, "
                    "_eex + (%s), _eey + (%s), _eez + (%s)); }"
                    % (idn, idn, idn, idn, rot_args, ex, ey, ez))
        i = end
    return "".join(out)


def _parse_quat_components(a, cl):
    """Quaternion value expr → (qx, qy, qz, qw) C exprs, or None."""
    a = a.strip()
    if re.match(r"(?:UnityEngine\.)?Quaternion\.identity\s*$", a):
        return ("0.f", "0.f", "0.f", "1.f")
    if re.match(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
            r"(?:rotation|localRotation)\s*$", a):
        idn = _c_ident(cl["name"])
        return ("_%s_rot_x[i]" % idn, "_%s_rot_y[i]" % idn,
                "_%s_rot_z[i]" % idn, "_%s_rot_w[i]" % idn)
    nm = re.match(r"new\s+Quaternion\s*\((.*)\)$", a, flags=re.S)
    if nm:
        args = _split_call_args(nm.group(1))
        if len(args) >= 4:
            return (args[0], args[1], args[2], args[3])
    # Nested Euler / LookRotation / identity via existing parser (non-slerp).
    if re.match(r"(?:UnityEngine\.)?Quaternion\.Slerp\s*\(", a):
        return None
    if re.match(r"(?:UnityEngine\.)?Quaternion\.Inverse\s*\(", a):
        return None
    if re.match(r"(?:UnityEngine\.)?Quaternion\.RotateTowards\s*\(", a):
        return None
    parsed = _parse_quaternion_expr(a, cl)
    if parsed and parsed[0] == "quat":
        return parsed[1]
    return None


def _parse_quaternion_expr(rhs, cl=None):
    """Parse Quaternion.Euler / LookRotation / Slerp / Inverse /
    RotateTowards / identity / new."""
    rhs = rhs.strip()
    if re.match(r"(?:UnityEngine\.)?Quaternion\.identity\s*$", rhs):
        return ("quat", ("0.f", "0.f", "0.f", "1.f"))
    em = re.match(r"(?:UnityEngine\.)?Quaternion\.Euler\s*\((.*)\)$",
                  rhs, flags=re.S)
    if em:
        args = _split_call_args(em.group(1))
        if len(args) == 3:
            return ("euler", (args[0], args[1], args[2]))
        if len(args) == 1:
            hit = _parse_vector3_expr(args[0])
            if hit:
                return ("euler", hit)
        return None
    lm = re.match(r"(?:UnityEngine\.)?Quaternion\.LookRotation\s*\((.*)\)$",
                  rhs, flags=re.S)
    if lm:
        args = _split_call_args(lm.group(1))
        if len(args) == 1:
            fwd = _parse_vector3_expr(args[0])
            if fwd:
                return ("look", (fwd, ("0.f", "1.f", "0.f")))
        elif len(args) == 2:
            fwd = _parse_vector3_expr(args[0])
            up = _parse_vector3_expr(args[1])
            if fwd and up:
                return ("look", (fwd, up))
        return None
    sm = re.match(r"(?:UnityEngine\.)?Quaternion\.Slerp\s*\((.*)\)$",
                  rhs, flags=re.S)
    if sm:
        if cl is None:
            return None
        args = _split_call_args(sm.group(1))
        if len(args) == 3:
            a = _parse_quat_components(args[0], cl)
            b = _parse_quat_components(args[1], cl)
            if a and b:
                return ("slerp", (a, b, args[2]))
        return None
    im = re.match(r"(?:UnityEngine\.)?Quaternion\.Inverse\s*\((.*)\)$",
                  rhs, flags=re.S)
    if im:
        if cl is None:
            return None
        args = _split_call_args(im.group(1))
        if len(args) == 1:
            q = _parse_quat_components(args[0], cl)
            if q:
                return ("inverse", q)
        return None
    rm = re.match(
        r"(?:UnityEngine\.)?Quaternion\.RotateTowards\s*\((.*)\)$",
        rhs, flags=re.S)
    if rm:
        if cl is None:
            return None
        args = _split_call_args(rm.group(1))
        if len(args) == 3:
            a = _parse_quat_components(args[0], cl)
            b = _parse_quat_components(args[1], cl)
            if a and b:
                return ("rotate_towards", (a, b, args[2]))
        return None
    nm = re.match(r"new\s+Quaternion\s*\((.*)\)$", rhs, flags=re.S)
    if nm:
        args = _split_call_args(nm.group(1))
        if len(args) >= 4:
            return ("quat", (args[0], args[1], args[2], args[3]))
    return None


def _rewrite_quaternion_angle(text, cl):
    """Lower Quaternion.Angle(a, b) → _engine_quat_angle(...components...).

    Returns degrees between two rotations (Unity). Args must lower via
    ``_parse_quat_components`` (identity / transform.rotation / new / …).
    """
    out = []
    i = 0
    pat = re.compile(r"(?<![\w.])(?:UnityEngine\.)?Quaternion\.Angle\s*\(")
    while i < len(text):
        m = pat.search(text, i)
        if not m:
            out.append(text[i:])
            break
        open_paren = m.end() - 1
        parsed = _match_call_args(text, open_paren)
        if not parsed:
            out.append(text[i:open_paren + 1])
            i = open_paren + 1
            continue
        args_str, after = parsed
        args = _split_call_args(args_str)
        out.append(text[i:m.start()])
        if len(args) != 2:
            out.append(text[m.start():after])
            i = after
            continue
        a = _parse_quat_components(args[0], cl)
        b = _parse_quat_components(args[1], cl)
        if not a or not b:
            out.append(text[m.start():after])
            i = after
            continue
        ax, ay, az, aw = a
        bx, by, bz, bw = b
        out.append(
            "_engine_quat_angle((%s), (%s), (%s), (%s), "
            "(%s), (%s), (%s), (%s))"
            % (ax, ay, az, aw, bx, by, bz, bw))
        i = after
    return "".join(out)


def _rewrite_transform_rotation(text, cl):
    """Lower transform.rotation|localRotation = Quaternion… → live quat.

    Supports:
      transform.rotation = Quaternion.Euler(x, y, z);
      transform.rotation = Quaternion.Euler(Vector3.forward * deg);
      transform.rotation = Quaternion.LookRotation(forward[, up]);
      transform.rotation = Quaternion.Slerp(a, b, t);
      transform.rotation = Quaternion.Inverse(q);
      transform.rotation = Quaternion.RotateTowards(a, b, maxDegreesDelta);
      transform.rotation = Quaternion.identity;
      transform.rotation = new Quaternion(x, y, z, w);
      transform.localRotation = … (same forms);
    Unparented bodies: world rotation ≈ local (packed live quat).
    """
    idn = _c_ident(cl["name"])
    rot_args = (
        "&_%s_rot_x[i], &_%s_rot_y[i], &_%s_rot_z[i], &_%s_rot_w[i], "
        "&_%s_rot_m00[i], &_%s_rot_m01[i], &_%s_rot_m10[i], "
        "&_%s_rot_m11[i]" % ((idn,) * 8)
    )
    out = []
    i = 0
    while i < len(text):
        m = re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
            r"(?:rotation|localRotation)\s*"
            r"=(?!=)",
            text[i:])
        if not m:
            out.append(text[i:])
            break
        start = i + m.start()
        rhs_start = i + m.end()
        j = rhs_start
        depth = 0
        while j < len(text):
            c = text[j]
            if c == '"':
                j = _skip_c_string(text, j)
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == ";" and depth == 0:
                break
            j += 1
        rhs = text[rhs_start:j].strip()
        end = j + 1 if j < len(text) and text[j] == ";" else j
        out.append(text[i:start])
        parsed = _parse_quaternion_expr(rhs, cl)
        if parsed is None:
            out.append(text[start:end])
        elif parsed[0] == "euler":
            ex, ey, ez = parsed[1]
            out.append(
                "_engine_transform_set_euler(%s, (%s), (%s), (%s));"
                % (rot_args, ex, ey, ez))
        elif parsed[0] == "look":
            (fx, fy, fz), (ux, uy, uz) = parsed[1]
            out.append(
                "_engine_quat_look_rotation(%s, (%s), (%s), (%s), "
                "(%s), (%s), (%s));"
                % (rot_args, fx, fy, fz, ux, uy, uz))
        elif parsed[0] == "slerp":
            (ax, ay, az, aw), (bx, by, bz, bw), t = parsed[1]
            out.append(
                "{ float _sqx, _sqy, _sqz, _sqw; "
                "_engine_quat_slerp((%s), (%s), (%s), (%s), "
                "(%s), (%s), (%s), (%s), (%s), "
                "&_sqx, &_sqy, &_sqz, &_sqw); "
                "_engine_transform_set_quat(%s, _sqx, _sqy, _sqz, _sqw); }"
                % (ax, ay, az, aw, bx, by, bz, bw, t, rot_args))
        elif parsed[0] == "inverse":
            qx, qy, qz, qw = parsed[1]
            out.append(
                "{ float _iqx, _iqy, _iqz, _iqw; "
                "_engine_quat_inverse((%s), (%s), (%s), (%s), "
                "&_iqx, &_iqy, &_iqz, &_iqw); "
                "_engine_transform_set_quat(%s, _iqx, _iqy, _iqz, _iqw); }"
                % (qx, qy, qz, qw, rot_args))
        elif parsed[0] == "rotate_towards":
            (ax, ay, az, aw), (bx, by, bz, bw), md = parsed[1]
            out.append(
                "{ float _rtx, _rty, _rtz, _rtw; "
                "_engine_quat_rotate_towards((%s), (%s), (%s), (%s), "
                "(%s), (%s), (%s), (%s), (%s), "
                "&_rtx, &_rty, &_rtz, &_rtw); "
                "_engine_transform_set_quat(%s, _rtx, _rty, _rtz, _rtw); }"
                % (ax, ay, az, aw, bx, by, bz, bw, md, rot_args))
        else:
            qx, qy, qz, qw = parsed[1]
            out.append(
                "_engine_transform_set_quat(%s, (%s), (%s), (%s), (%s));"
                % (rot_args, qx, qy, qz, qw))
        i = end
    return "".join(out)


def _rewrite_local_rotation_reads(text, cl, plan):
    """Lower transform.localRotation.x|y|z|w → live quat tables."""
    if cl["name"] not in set(plan.get("live_rot_classes") or []):
        return text
    idn = _c_ident(cl["name"])
    for axis in ("x", "y", "z", "w"):
        text = re.sub(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*localRotation\s*\.\s*"
            + axis + r"\b",
            "_%s_rot_%s[i]" % (idn, axis),
            text)
    return text


def _skip_c_string(text, i):
    """Index just past a C/C# string literal starting at text[i] == '\"'."""
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == '"':
            return j + 1
        j += 1
    return j


def _parse_plus_rhs(text, i):
    """Scan one + operand starting at *i*; stop at top-level + , ) ;."""
    while i < len(text) and text[i] in " \t\n\r":
        i += 1
    start = i
    depth = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            i = _skip_c_string(text, i)
            continue
        if c == "'":
            i += 1
            if i < len(text) and text[i] == "\\":
                i += 2
            elif i < len(text):
                i += 1
            if i < len(text) and text[i] == "'":
                i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                break
            depth -= 1
        elif c in ",;" and depth == 0:
            break
        elif c == "+" and depth == 0:
            break
        i += 1
    return start, i


def _rewrite_string_concat(text, string_idents=None):
    """Rewrite C# string + value to typed _str_plus_* (C pointer + is wrong).

    Handles `"lit" + expr`, `Application_*Path() + expr`, and chains via
    repeated `_str_plus_*(...) + expr`. `string_idents` are bare names known
    to be `string` (class static/const fields) so RHS picks `_str_plus_s`.
    """
    string_idents = frozenset(string_idents or ())
    changed = True
    while changed:
        changed = False
        out = []
        i = 0
        while i < len(text):
            left = None
            left_end = None
            m_plus = re.match(r"_str_plus_[ifcs]\(", text[i:])
            m_app = re.match(
                r"Application_(?:dataPath|persistentDataPath|productName)\(\)",
                text[i:])
            if m_plus or text.startswith("_str_plus(", i):
                prefix = m_plus.group(0) if m_plus else "_str_plus("
                depth = 0
                j = i + len(prefix) - 1
                while j < len(text):
                    if text[j] == '"':
                        j = _skip_c_string(text, j)
                        continue
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    j += 1
                left = text[i:j]
                left_end = j
            elif m_app:
                left = m_app.group(0)
                left_end = i + len(left)
            elif text[i] == '"':
                j = _skip_c_string(text, i)
                left = text[i:j]
                left_end = j
            if left is not None:
                k = left_end
                while k < len(text) and text[k] in " \t\n\r":
                    k += 1
                if k < len(text) and text[k] == "+":
                    rhs_start, rhs_end = _parse_plus_rhs(text, k + 1)
                    rhs = text[rhs_start:rhs_end].strip()
                    if rhs:
                        kind = _c_expr_scalar_kind(
                            rhs, string_idents=string_idents)
                        out.append("_str_plus_%s(%s, (%s))"
                                   % (kind, left, rhs))
                        i = rhs_end
                        changed = True
                        continue
            out.append(text[i])
            i += 1
        text = "".join(out)
    return text


def _c_expr_scalar_kind(expr, string_idents=None):
    """Pick i/f/c/s suffix for Debug_Log / Console_WriteLine / _str_plus."""
    string_idents = frozenset(string_idents or ())
    e = expr.strip()
    while (e.startswith("(") and e.endswith(")")
           and e.count("(") == e.count(")")):
        inner = e[1:-1].strip()
        if not inner:
            break
        e = inner
    if (e.startswith('"') or e.startswith("_str_plus")
            or "ToString" in e or e.startswith("(const char")
            or e in ("Application_dataPath()",
                     "Application_persistentDataPath()",
                     "Application_productName()")
            or e.startswith("StreamReader_ReadLine(")
            or (re.match(r"^\w+$", e) and e in string_idents)):
        return "s"
    if re.match(r"^'(?:[^'\\]|\\.)'$", e):
        return "c"
    if re.match(r"^-?\d+$", e):
        return "i"
    return "f"


def _rewrite_typed_call_name(text, name, string_idents=None):
    """Rewrite Name(arg) → Name_{i,f,s}(arg). Skips already-typed Names."""
    out = []
    i = 0
    pat = re.compile(r"(?<![\w])%s(?!_[ifs]\b)\s*\(" % re.escape(name))
    while True:
        m = pat.search(text[i:])
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:i + m.start()])
        start = i + m.end()
        depth = 1
        j = start
        while j < len(text) and depth:
            c = text[j]
            if c == '"':
                j = _skip_c_string(text, j)
                continue
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
        args = text[start:j]
        kind = _c_expr_scalar_kind(args, string_idents=string_idents)
        out.append("%s_%s(%s)" % (name, kind, args))
        i = j + 1
    return "".join(out)


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


def _rewrite_csharp_float_literals(text):
    """C# `0f` → C `0.f`. C rejects a float suffix on an integer constant.

    C# allows `0f` / `1F` (digits + real-type-suffix). C needs a decimal
    point (`0.f` / `0.0f`). Literals that already have `.` or an exponent
    (`1.5f`, `1e2f`) are valid in both and left alone. Runs on a blanked
    scan so `"0f"` in a string stays put.
    """
    scan = cs2cpp._blank(text)
    out = []
    pos = 0
    for m in re.finditer(r"(?<![\w.])(\d+)([fF])\b", scan):
        out.append(text[pos:m.start(1)])
        out.append(m.group(1) + "." + m.group(2))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _parse_byte_array_lit_inner(inner):
    """Parse `1, 2, 0xFF` inside `new byte[] { ... }` → list of 0..255 ints."""
    nums = []
    for part in (inner or "").split(","):
        part = part.strip()
        if not part:
            continue
        part = re.sub(r"[fFdDmMuUlL]+$", "", part)
        if re.match(r"0[xX][0-9a-fA-F]+$", part):
            nums.append(int(part, 16) & 0xFF)
        elif re.match(r"0[bB][01]+$", part):
            nums.append(int(part, 2) & 0xFF)
        elif "." in part:
            nums.append(int(float(part)) & 0xFF)
        else:
            nums.append(int(part, 10) & 0xFF)
    return nums


_NEW_BYTE_ARRAY_LIT = re.compile(
    r"new\s+byte\s*\[\s*\]\s*\{([^}]*)\}", re.S)


def _collect_byte_array_lits(plan, analyses):
    """All `new byte[] { ... }` literals in emitted method bodies (stable order).

    Order matches emit_engine: sorted class names, then methods_by pairs,
    skipping Awake/OnEnable and ctor_forbidden scripts (same as lowering).
    """
    methods_by = {}
    for a in analyses or []:
        for c in a.get("classes") or []:
            methods_by.setdefault(c["name"], []).extend(
                [(c, m) for m in c.get("methods") or []])
    lits = []
    for cname, cl in sorted((plan.get("classes") or {}).items()):
        if cl.get("ctor_forbidden"):
            continue
        for _c, m in methods_by.get(cname, []):
            if m.get("name") in ("Awake", "OnEnable"):
                continue
            body = m.get("body") or ""
            for match in _NEW_BYTE_ARRAY_LIT.finditer(body):
                lits.append(_parse_byte_array_lit_inner(match.group(1)))
    return lits


def _rewrite_byte_array_lits(text, plan):
    """new byte[] { a, b } → _engine_ba_N() (helpers emitted in engine.c)."""
    counter = plan.get("_byte_array_lit_i")
    if counter is None:
        return text

    def repl(_m):
        i = counter[0]
        counter[0] = i + 1
        return "_engine_ba_%d()" % i

    return _NEW_BYTE_ARRAY_LIT.sub(repl, text)


def _rewrite_bytearray_member_access(text):
    """ByteArray locals: .Length → .length; [i] → .data[i]."""
    names = set(re.findall(r"\bByteArray\s+(\w+)\b", text))
    for name in names:
        text = re.sub(
            r"(?<![.\w])%s\.Length\b" % re.escape(name),
            "%s.length" % name, text)
        text = re.sub(
            r"(?<![.\w])%s\s*\[(.*?)\]" % re.escape(name),
            r"%s.data[\1]" % name, text)
    return text


def _rewrite_file_copy(text):
    """File.Copy(src, dest) / Copy(src, dest, overwrite) → File_Copy(..., int)."""
    if not re.search(r"(?:System\.IO\.)?File\.Copy\s*\(", text):
        return text
    out = []
    i = 0
    pat = re.compile(r"(?:System\.IO\.)?File\.Copy\s*\(")
    while True:
        m = pat.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        start = m.end()
        depth = 1
        j = start
        while j < len(text) and depth:
            c = text[j]
            if c == '"':
                j = _skip_c_string(text, j)
                continue
            if c == "'":
                j += 1
                if j < len(text) and text[j] == "\\":
                    j += 2
                elif j < len(text):
                    j += 1
                if j < len(text) and text[j] == "'":
                    j += 1
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            out.append(text[m.start():])
            break
        args = _split_call_args(text[start:j])
        if len(args) == 2:
            args.append("0")
        elif len(args) >= 3:
            ov = args[2].strip()
            if ov == "true":
                args[2] = "1"
            elif ov == "false":
                args[2] = "0"
        out.append("File_Copy(%s)" % ", ".join(args[:3]))
        i = j + 1
    return "".join(out)


def _rewrite_file_text_streams(text, cl):
    """File.CreateText/OpenText + StreamWriter/Reader WriteLine/ReadLine/Close."""
    if not re.search(
            r"(?:System\.IO\.)?File\.(?:CreateText|OpenText)\s*\("
            r"|\b(?:StreamWriter|StreamReader)\b"
            r"|File_CreateText\(|File_OpenText\(",
            text):
        return text

    stream_names = set(re.findall(
        r"\b(?:StreamWriter|StreamReader)\s+(\w+)\b", text))
    for f in cl.get("class_consts") or []:
        if f.get("ty") in ("StreamWriter", "StreamReader"):
            stream_names.add(f["name"])

    text = re.sub(
        r"(?:System\.IO\.)?File\.CreateText\s*\(",
        "File_CreateText(", text)
    text = re.sub(
        r"(?:System\.IO\.)?File\.OpenText\s*\(",
        "File_OpenText(", text)
    text = re.sub(r"\bStreamWriter\b", "FILE *", text)
    text = re.sub(r"\bStreamReader\b", "FILE *", text)

    for name in sorted(stream_names, key=len, reverse=True):
        text = re.sub(
            r"(?<![.\w])%s\s*\.\s*WriteLine\s*\(" % re.escape(name),
            "StreamWriter_WriteLine(%s, " % name, text)
        text = re.sub(
            r"(?<![.\w])%s\s*\.\s*ReadLine\s*\(\s*\)" % re.escape(name),
            "StreamReader_ReadLine(%s)" % name, text)
        text = re.sub(
            r"(?<![.\w])%s\s*\.\s*Close\s*\(\s*\)" % re.escape(name),
            "Stream_Close(%s)" % name, text)
    return text


def _lower_method_body(body, cl, plan, site=None, collision2d_param=None):
    """C# subset method → C against packed arrays.

    `this` / implicit fields become `_Class_inst_array[i].field`.
    `transform.position.x` becomes the packed pos_*. A class-typed
    field is already an index: `other.hp` → `_Other_inst_array[other].hp`.
    """
    idn = _c_ident(cl["name"])
    text = _rewrite_csharp_float_literals(body)
    text = re.sub(r"\bthis\.", "", text)
    text = _rewrite_byte_array_lits(text, plan)
    text = _rewrite_file_copy(text)
    text = _rewrite_file_text_streams(text, cl)
    text = _rewrite_extensions_set_world_scale(text, cl, plan)
    text = _rewrite_rigidbody_assigns(text, plan, cl["name"])
    text = _rewrite_transform_rotate(text, cl)
    text = _rewrite_transform_look_at(text, cl)
    text = _rewrite_transform_euler_angles(text, cl)
    text = _rewrite_transform_rotation(text, cl)
    text = _rewrite_quaternion_angle(text, cl)
    text = _rewrite_local_rotation_reads(text, cl, plan)
    text = _rewrite_transform_parent(text, cl, plan)
    text = _rewrite_transform_set_parent(text, cl, plan)
    text = _rewrite_transform_get_sibling_index(text, cl, plan)
    text = _rewrite_transform_game_object(text, cl, plan)
    text = _rewrite_transform_point(text, cl, plan)
    text = _rewrite_transform_matrices(text, cl, plan)
    text = _rewrite_transform_find(text, cl, plan)
    text = _rewrite_local_position_vec3_fields(text, cl)
    # GameObject ≡ Transform index: drop redundant .transform on Find / GO.
    if plan.get("go_names"):
        text = re.sub(
            r"((?:GameObject\s*\.\s*Find|GameObject_Find)\s*\(\s*[^)]*\s*\))"
            r"\s*\.\s*transform\b",
            r"\1", text)
    # Unity Object null checks → packed index sentinel (-1).
    if plan.get("go_names"):
        text = re.sub(r"==\s*null\b", "== -1", text)
        text = re.sub(r"!=\s*null\b", "!= -1", text)
        # Transform / GameObject locals are GO indices.
        text = re.sub(r"\bTransform\b(?=\s+\w)", "int", text)
        text = re.sub(r"\bGameObject\b(?=\s+\w)", "int", text)
    # C# string locals → const char * (ReadLine / path vars).
    text = re.sub(r"\bstring\b(?=\s+\w)", "const char *", text)
    # Find/GetComponent before field rewrites so `.amp` stays on the target type.
    text = _rewrite_find_getcomponent(text, plan, cl["name"], site=site)
    text, add_locals = _rewrite_addcomponent(text, plan, cl["name"])
    text = _rewrite_audiosource_api(text, cl, add_locals=add_locals)
    # AudioSource / authored UI component locals are packed indices.
    text = re.sub(r"\bAudioSource\b(?=\s+\w)", "int", text)
    for ui_ty in sorted(_UI_GETCOMPONENT_TYPES, key=len, reverse=True):
        text = re.sub(
            r"\b%s\b(?=\s+\w)" % re.escape(ui_ty), "int", text)
    # API tokens before Vector2 rewrites so nested Mathf.Sin(...) keeps parens.
    text = text.replace("Time.deltaTime", "Time_deltaTime")
    text = text.replace("Time.fixedDeltaTime", "Time_fixedDeltaTime")
    text = text.replace("Time.time", "Time_time")
    text = text.replace("Screen.width", "Screen_width")
    text = text.replace("Screen.height", "Screen_height")
    text = re.sub(
        r"(?<![\w])(?:UnityEngine\.)?Application\.dataPath\b",
        "Application_dataPath()", text)
    text = re.sub(
        r"(?<![\w])(?:UnityEngine\.)?Application\.persistentDataPath\b",
        "Application_persistentDataPath()", text)
    text = re.sub(
        r"(?<![\w])(?:UnityEngine\.)?Application\.isEditor\b",
        "Application_isEditor()", text)
    text = re.sub(
        r"(?<![\w])(?:UnityEngine\.)?Application\.isPlaying\b",
        "Application_isPlaying()", text)
    text = re.sub(
        r"(?<![\w])(?:UnityEngine\.)?Application\.productName\b",
        "Application_productName()", text)
    text = re.sub(
        r"(?<![\w])(?:UnityEngine\.)?Application\.OpenURL\s*\(",
        "Application_OpenURL(", text)
    # Quit() → Quit(0); Quit(code) keeps the arg.
    text = re.sub(
        r"(?<![\w])(?:UnityEngine\.)?Application\.Quit\s*\(\s*\)",
        "Application_Quit(0)", text)
    text = re.sub(
        r"(?<![\w])(?:UnityEngine\.)?Application\.Quit\s*\(",
        "Application_Quit(", text)
    text = re.sub(
        r"(?:System\.IO\.)?File\.WriteAllText\s*\(",
        "File_WriteAllText(", text)
    text = re.sub(
        r"(?:System\.IO\.)?File\.AppendAllText\s*\(",
        "File_AppendAllText(", text)
    text = re.sub(
        r"(?:System\.IO\.)?File\.WriteAllBytes\s*\(",
        "File_WriteAllBytes(", text)
    text = re.sub(
        r"(?:System\.IO\.)?File\.ReadAllBytes\s*\(",
        "File_ReadAllBytes(", text)
    text = re.sub(
        r"(?:System\.IO\.)?File\.Exists\s*\(",
        "File_Exists(", text)
    text = re.sub(
        r"(?:System\.IO\.)?File\.Delete\s*\(",
        "File_Delete(", text)
    # byte[] locals / params → ByteArray (File WriteAllBytes / ReadAllBytes).
    if plan.get("_byte_array_lit_i") is not None:
        text = re.sub(r"\bbyte\s*\[\s*\]", "ByteArray", text)
        text = _rewrite_bytearray_member_access(text)
    text = re.sub(
        r"(?<![\w.])(?:Object\.)?Destroy\s*\(\s*gameObject\s*\)",
        "Object_Destroy(_engine_go_of_%s(i))" % idn
        if plan.get("go_names") else "Object_Destroy(-1)",
        text)
    text = re.sub(
        r"(?<![\w.])(?:Object\.)?Destroy\s*\(\s*this\s*\)",
        "Object_Destroy(_engine_go_of_%s(i))" % idn
        if plan.get("go_names") else "Object_Destroy(-1)",
        text)
    text = text.replace("Physics2D.gravity.x", "Physics2D_gravity_x")
    text = text.replace("Physics2D.gravity.y", "Physics2D_gravity_y")
    text = text.replace("Physics.gravity.x", "Physics_gravity_x")
    text = text.replace("Physics.gravity.y", "Physics_gravity_y")
    text = text.replace("Physics.gravity.z", "Physics_gravity_z")
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
    text = text.replace("Camera.main.transform.position.z",
                        "Camera_main_pos_z")
    text = text.replace("Camera.main.nearClipPlane",
                        "Camera_main_nearClipPlane")
    text = text.replace("Camera.main.farClipPlane",
                        "Camera_main_farClipPlane")
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
    string_idents = {
        f["name"] for f in (cl.get("class_consts") or [])
        if f.get("ty") == "string"
    }
    # Locals: `string x` / `const char *x` (after string→const char * rewrite).
    string_idents |= set(re.findall(
        r"\b(?:string|const char \*)\s+(\w+)\b", text))
    text = _rewrite_string_concat(text, string_idents=string_idents)
    # Unity Object.ToString when printing a Find result (name, not index).
    text = _wrap_log_gameobject_tostring(text)
    text = _wrap_log_component_tostring(text, add_locals)
    text = _wrap_log_collision2d_tostring(text, collision2d_param)
    text = re.sub(r"Mathf\.(Abs|Min|Max|Clamp|Lerp|Sin|Cos|Sign)\s*\(",
                  lambda m: "Mathf_%s(" % m.group(1), text)
    text = re.sub(r"transform\.position\.x", idn + "_get_pos_x(i)", text)
    text = re.sub(r"transform\.position\.y", idn + "_get_pos_y(i)", text)
    text = re.sub(r"transform\.position\.z",
                  idn + "_get_pos_z(i)" if not cl["two_d"] else "0.f", text)
    # Packed pos is local under a live parent, else world — matches Unity
    # localPosition when parented / unparented respectively for our storage.
    text = re.sub(r"transform\.localPosition\.x", idn + "_get_pos_x(i)", text)
    text = re.sub(r"transform\.localPosition\.y", idn + "_get_pos_y(i)", text)
    text = re.sub(r"transform\.localPosition\.z",
                  idn + "_get_pos_z(i)" if not cl["two_d"] else "0.f", text)
    text = _rewrite_new_vector_assigns(text, idn, two_d=bool(cl.get("two_d")))

    members = {n for n, _t, _b, _k in cl["members"]}
    # Class const / static names (FRAME_CNT, LOG_FILE_PATH).
    class_const_names = {
        f["name"]: f for f in (cl.get("class_consts") or [])
    }
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
    for vf in cl.get("vec3_fields") or []:
        text = re.sub(r"(?<![_\w])%s\.x\b" % vf, "%s_x" % vf, text)
        text = re.sub(r"(?<![_\w])%s\.y\b" % vf, "%s_y" % vf, text)
        text = re.sub(r"(?<![_\w])%s\.z\b" % vf, "%s_z" % vf, text)
        # Property PascalCase → field (Offset → offset) for TransformPoint args.
        prop = vf[:1].upper() + vf[1:] if vf else vf
        if prop != vf:
            text = re.sub(r"(?<![_\w])%s\.x\b" % prop, "%s_x" % vf, text)
            text = re.sub(r"(?<![_\w])%s\.y\b" % prop, "%s_y" % vf, text)
            text = re.sub(r"(?<![_\w])%s\.z\b" % prop, "%s_z" % vf, text)
            text = re.sub(
                r"(?<![_\w])%s(?![\w])" % prop,
                vf, text)
    # Const/static class fields before instance member rewrites.
    for name in sorted(class_const_names, key=len, reverse=True):
        text = re.sub(
            r"(?<![_\w])%s(?![\w])" % name,
            "%s_%s" % (idn, name),
            text)
    for name in sorted(members, key=len, reverse=True):
        # ++ / -- before assignment rewrites.
        text = re.sub(
            r"(?<![_\w])%s\s*\+\+" % name,
            "%s_set_%s(i, %s_get_%s(i) + 1)" % (idn, name, idn, name),
            text)
        text = re.sub(
            r"(?<![_\w])%s\s*--" % name,
            "%s_set_%s(i, %s_get_%s(i) - 1)" % (idn, name, idn, name),
            text)
        text = re.sub(
            r"\+\+\s*(?<![_\w])%s(?![\w])" % name,
            "%s_set_%s(i, %s_get_%s(i) + 1)" % (idn, name, idn, name),
            text)
        text = re.sub(
            r"--\s*(?<![_\w])%s(?![\w])" % name,
            "%s_set_%s(i, %s_get_%s(i) - 1)" % (idn, name, idn, name),
            text)
        text = re.sub(
            r"(?<![_\w])%s\s*\+=" % name,
            "%s_set_%s(i, %s_get_%s(i) +" % (idn, name, idn, name),
            text)
        text = re.sub(
            r"(?<![_\w])%s\s*-=" % name,
            "%s_set_%s(i, %s_get_%s(i) -" % (idn, name, idn, name),
            text)
        # Assignment: `=` but not `==` / `!=` / `<=` / `>=`.
        text = re.sub(
            r"(?<![_\w])%s\s*=(?!=)" % name,
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
    # Skip builtins (AudioSource / Rigidbody*) — their props lower earlier.
    for name, _ty, _bits, kind in cl["members"]:
        if not str(kind).startswith("idx:"):
            continue
        other = kind.split(":", 1)[1]
        if (other in _ADDABLE_BUILTINS or other in _PHYSICS_COMPONENTS):
            continue
        oiden = _c_ident(other)
        text = re.sub(
            r"\b%s\.(\w+)" % name,
            lambda m: "%s_AT(%s_get_%s(i)).%s" % (
                oiden, idn, name, m.group(1)),
            text)
    # Typed Debug_Log / Console_WriteLine — crust has no _Generic.
    text = _rewrite_typed_call_name(
        text, "Debug_Log", string_idents=string_idents)
    text = _rewrite_typed_call_name(
        text, "Console_WriteLine", string_idents=string_idents)
    return text


def emit_data(plan, used_apis=None):
    lines = []
    p = lines.append
    used_apis = used_apis or set()
    rb2d_list = plan.get("rigidbody2d") or []
    rb3d_list = plan.get("rigidbody") or []
    add_budget = plan.get("addcomponent_budget") or {}
    add_types = set(plan.get("addcomponent_types") or [])
    rb2d_cap = len(rb2d_list) + int(add_budget.get("Rigidbody2D") or 0)
    rb3d_cap = len(rb3d_list) + int(add_budget.get("Rigidbody") or 0)
    light_budget = int(add_budget.get("Light") or 0)
    want_phys = "Physics2D.gravity" in used_apis or bool(rb2d_list) or (
        "Rigidbody2D" in add_types)
    want_phys3 = "Physics.gravity" in used_apis or bool(rb3d_list) or (
        "Rigidbody" in add_types)
    want_input = bool(used_apis & _WANT_INPUT)
    want_keyboard = "Keyboard.current" in used_apis
    keyboard_keys = set(plan.get("keyboard_keys") or [])
    want_ambient = "RenderSettings.ambientLight" in used_apis or (
        "Light" in add_types)
    lights = plan.get("lights") or []
    light_cap = len(lights) + light_budget
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    p("/* generated by tools/unity_pack.py — scene tables, compile -O0 */")
    if plan.get("soa"):
        p("/* SoA: positions live in _Class_pos[N][dims], not in the struct */")
    p("#include <stdint.h>")
    p("")
    # Player Settings → Screen.* (hosts use these for window size).
    p("int Screen_width = %d;" % int(plan.get("screen_width") or 1024))
    p("int Screen_height = %d;" % int(plan.get("screen_height") or 768))
    p("int Screen_fullScreen = %d;" % int(plan.get("screen_fullscreen") or 0))
    p("int Screen_fullScreenNative = %d;" % int(
        plan.get("screen_fullscreen_native") if plan.get(
            "screen_fullscreen_native") is not None else 1))
    p("int Screen_maximized = %d;" % int(plan.get("screen_maximized") or 0))
    p("const char engine_product_name[] = %s;" % _c_string(
        plan.get("product_name") or "Player"))
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
    p("float Time_fixedDeltaTime = 0.02f;")
    if want_phys:
        p("float Physics2D_gravity_x = 0.f;")
        p("float Physics2D_gravity_y = -9.81f;")
    if want_phys3:
        p("float Physics_gravity_x = 0.f;")
        p("float Physics_gravity_y = -9.81f;")
        p("float Physics_gravity_z = 0.f;")
    if want_ambient:
        # Unity default ambient-ish grey; host may override.
        p("float RenderSettings_ambient_r = 0.2f;")
        p("float RenderSettings_ambient_g = 0.2f;")
        p("float RenderSettings_ambient_b = 0.2f;")
    cam = plan.get("camera")
    if cam:
        p("float Camera_main_pos_x = %sf;" % repr(float(cam["pos"][0])))
        p("float Camera_main_pos_y = %sf;" % repr(float(cam["pos"][1])))
        p("float Camera_main_pos_z = %sf;" % repr(float(cam["pos"][2])))
        p("float Camera_main_orthographicSize = %sf;" % repr(
            float(cam["orthographic_size"])))
        p("float Camera_main_nearClipPlane = %sf;" % repr(
            float(cam.get("near_clip", 0.3))))
        p("float Camera_main_farClipPlane = %sf;" % repr(
            float(cam.get("far_clip", 1000.0))))
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
    if plan.get("ui_buttons"):
        p("/* Host: screen-space pointer (origin bottom-left, y up). */")
        p("float engine_pointer_x = 0.f;")
        p("float engine_pointer_y = 0.f;")
        p("int engine_pointer_down = 0;")
    if light_cap:
        p("int _Light_count = %d;" % len(lights))
        intens = [float(L["intensity"]) for L in lights] + [1.0] * light_budget
        cr = [float(L["r"]) for L in lights] + [1.0] * light_budget
        cg = [float(L["g"]) for L in lights] + [1.0] * light_budget
        cb = [float(L["b"]) for L in lights] + [1.0] * light_budget
        p("float _Light_intensity[%d] = { %s };" % (
            light_cap,
            ", ".join("%sf" % repr(v) for v in intens)))
        p("float _Light_color_r[%d] = { %s };" % (
            light_cap, ", ".join("%sf" % repr(v) for v in cr)))
        p("float _Light_color_g[%d] = { %s };" % (
            light_cap, ", ".join("%sf" % repr(v) for v in cg)))
        p("float _Light_color_b[%d] = { %s };" % (
            light_cap, ", ".join("%sf" % repr(v) for v in cb)))
    if rb2d_cap:
        n = len(rb2d_list)
        cap = rb2d_cap
        p("int _Rigidbody2D_count = %d;" % n)
        def _pad_f(vals, fill=0.0):
            return vals + [fill] * (cap - len(vals))
        def _pad_i(vals, fill=0):
            return vals + [fill] * (cap - len(vals))
        p("float _Rigidbody2D_vel_x[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f(
                [r["vel_x"] for r in rb2d_list]))))
        p("float _Rigidbody2D_vel_y[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f(
                [r["vel_y"] for r in rb2d_list]))))
        p("float _Rigidbody2D_gravity_scale[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f(
                [r["gravity_scale"] for r in rb2d_list], 1.0))))
        p("float _Rigidbody2D_linear_damping[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f(
                [r.get("linear_damping", 0.0) for r in rb2d_list]))))
        p("float _Rigidbody2D_mass[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f(
                [r["mass"] for r in rb2d_list], 1.0))))
        p("int _Rigidbody2D_body_type[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i(
                [r["body_type"] for r in rb2d_list]))))
        p("int _Rigidbody2D_owner_class[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i(
                [class_ids[r["owner_class"]] for r in rb2d_list], -1))))
        p("int _Rigidbody2D_owner_inst[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i(
                [r["owner_inst"] for r in rb2d_list]))))
    asrc_list = plan.get("audiosources") or []
    as_add = int((plan.get("addcomponent_budget") or {}).get("AudioSource") or 0)
    as_cap = len(asrc_list) + as_add
    if as_cap or "AudioSource" in (plan.get("addcomponent_types") or []):
        as_cap = max(1, as_cap)
        n = len(asrc_list)

        def _pad_as(vals, fill=0):
            return list(vals) + [fill] * (as_cap - len(vals))

        def _pad_asf(vals, fill=0.0):
            return list(vals) + [fill] * (as_cap - len(vals))

        p("int _AudioSource_count = %d;" % n)
        go_names = plan.get("go_names") or []
        name_i = {nm: i for i, nm in enumerate(go_names)}
        owner_gos = []
        for r in asrc_list:
            owner_gos.append(int(name_i.get(r.get("name") or "", -1)))
        p("int _AudioSource_owner_go[%d] = { %s };" % (
            as_cap, ", ".join(str(int(v)) for v in _pad_as(owner_gos, -1))))
        p("int _AudioSource_play_on_awake[%d] = { %s };" % (
            as_cap, ", ".join(str(int(v)) for v in _pad_as(
                [r["play_on_awake"] for r in asrc_list], 1))))
        p("int _AudioSource_loop[%d] = { %s };" % (
            as_cap, ", ".join(str(int(v)) for v in _pad_as(
                [r["loop"] for r in asrc_list]))))
        p("int _AudioSource_mute[%d] = { %s };" % (
            as_cap, ", ".join(str(int(v)) for v in _pad_as(
                [r.get("mute", 0) for r in asrc_list]))))
        p("float _AudioSource_volume[%d] = { %s };" % (
            as_cap, ", ".join("%sf" % repr(float(v)) for v in _pad_asf(
                [r["volume"] for r in asrc_list], 1.0))))
        p("float _AudioSource_pitch[%d] = { %s };" % (
            as_cap, ", ".join("%sf" % repr(float(v)) for v in _pad_asf(
                [r["pitch"] for r in asrc_list], 1.0))))
        p("int _AudioSource_clip[%d] = { %s };" % (
            as_cap, ", ".join(str(int(v)) for v in _pad_as(
                [r["clip"] for r in asrc_list], -1))))
        p("int _AudioSource_playing[%d] = { %s };" % (
            as_cap, ", ".join(str(int(v)) for v in _pad_as(
                [r.get("playing", 0) for r in asrc_list]))))
    if rb3d_cap:
        n = len(rb3d_list)
        cap = rb3d_cap
        p("int _Rigidbody_count = %d;" % n)
        def _pad_f3(vals, fill=0.0):
            return vals + [fill] * (cap - len(vals))
        def _pad_i3(vals, fill=0):
            return vals + [fill] * (cap - len(vals))
        p("float _Rigidbody_vel_x[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f3(
                [r["vel_x"] for r in rb3d_list]))))
        p("float _Rigidbody_vel_y[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f3(
                [r["vel_y"] for r in rb3d_list]))))
        p("float _Rigidbody_vel_z[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f3(
                [r["vel_z"] for r in rb3d_list]))))
        p("float _Rigidbody_mass[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f3(
                [r["mass"] for r in rb3d_list], 1.0))))
        p("float _Rigidbody_drag[%d] = { %s };" % (
            cap, ", ".join("%sf" % repr(float(v)) for v in _pad_f3(
                [r.get("drag", 0.0) for r in rb3d_list]))))
        p("int _Rigidbody_use_gravity[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i3(
                [r["use_gravity"] for r in rb3d_list], 1))))
        p("int _Rigidbody_owner_class[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i3(
                [class_ids[r["owner_class"]] for r in rb3d_list], -1))))
        p("int _Rigidbody_owner_inst[%d] = { %s };" % (
            cap, ", ".join(str(int(v)) for v in _pad_i3(
                [r["owner_inst"] for r in rb3d_list]))))
    col2d_list = plan.get("collider2d") or []
    if col2d_list:
        n = len(col2d_list)
        p("const int _Collider2D_count = %d;" % n)
        p("const int _Collider2D_kind[%d] = { %s };" % (
            n, ", ".join(str(int(c["kind"])) for c in col2d_list)))
        p("const int _Collider2D_is_trigger[%d] = { %s };" % (
            n, ", ".join(str(int(c["is_trigger"])) for c in col2d_list)))
        p("const int _Collider2D_body_type[%d] = { %s };" % (
            n, ", ".join(str(int(c["body_type"])) for c in col2d_list)))
        p("const int _Collider2D_owner_class[%d] = { %s };" % (
            n, ", ".join(str(int(c["owner_class_id"])) for c in col2d_list)))
        p("const int _Collider2D_owner_inst[%d] = { %s };" % (
            n, ", ".join(str(int(c["owner_inst"])) for c in col2d_list)))
        p("const int _Collider2D_rb2d[%d] = { %s };" % (
            n, ", ".join(str(int(c["rb2d"])) for c in col2d_list)))
        p("const float _Collider2D_ox[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["ox"])) for c in col2d_list)))
        p("const float _Collider2D_oy[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["oy"])) for c in col2d_list)))
        p("const float _Collider2D_hw[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["hw"])) for c in col2d_list)))
        p("const float _Collider2D_hh[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["hh"])) for c in col2d_list)))
        p("const float _Collider2D_cos[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["cos_z"])) for c in col2d_list)))
        p("const float _Collider2D_sin[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["sin_z"])) for c in col2d_list)))
        p("const float _Collider2D_friction[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["friction"]))
                         for c in col2d_list)))
        p("const float _Collider2D_bounciness[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["bounciness"]))
                         for c in col2d_list)))
        p("const int _Collider2D_friction_combine[%d] = { %s };" % (
            n, ", ".join(str(int(c["friction_combine"]))
                         for c in col2d_list)))
        p("const int _Collider2D_bounce_combine[%d] = { %s };" % (
            n, ", ".join(str(int(c["bounce_combine"])) for c in col2d_list)))
    col3d_list = plan.get("collider3d") or []
    if col3d_list:
        n = len(col3d_list)
        p("const int _Collider3D_count = %d;" % n)
        p("const int _Collider3D_kind[%d] = { %s };" % (
            n, ", ".join(str(int(c["kind"])) for c in col3d_list)))
        p("const int _Collider3D_is_trigger[%d] = { %s };" % (
            n, ", ".join(str(int(c["is_trigger"])) for c in col3d_list)))
        p("const int _Collider3D_body_type[%d] = { %s };" % (
            n, ", ".join(str(int(c["body_type"])) for c in col3d_list)))
        p("const int _Collider3D_owner_class[%d] = { %s };" % (
            n, ", ".join(str(int(c["owner_class_id"])) for c in col3d_list)))
        p("const int _Collider3D_owner_inst[%d] = { %s };" % (
            n, ", ".join(str(int(c["owner_inst"])) for c in col3d_list)))
        p("const int _Collider3D_rb3d[%d] = { %s };" % (
            n, ", ".join(str(int(c["rb3d"])) for c in col3d_list)))
        p("const float _Collider3D_ox[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["ox"])) for c in col3d_list)))
        p("const float _Collider3D_oy[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["oy"])) for c in col3d_list)))
        p("const float _Collider3D_oz[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["oz"])) for c in col3d_list)))
        p("const float _Collider3D_hw[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["hw"])) for c in col3d_list)))
        p("const float _Collider3D_hh[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["hh"])) for c in col3d_list)))
        p("const float _Collider3D_hd[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["hd"])) for c in col3d_list)))
        p("const float _Collider3D_dynamic_friction[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["dynamic_friction"]))
                         for c in col3d_list)))
        p("const float _Collider3D_static_friction[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["static_friction"]))
                         for c in col3d_list)))
        p("const float _Collider3D_bounciness[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(c["bounciness"]))
                         for c in col3d_list)))
        p("const int _Collider3D_friction_combine[%d] = { %s };" % (
            n, ", ".join(str(int(c["friction_combine"]))
                         for c in col3d_list)))
        p("const int _Collider3D_bounce_combine[%d] = { %s };" % (
            n, ", ".join(str(int(c["bounce_combine"])) for c in col3d_list)))
    anim_plan = plan.get("animation") or {}
    anim_players = anim_plan.get("players") or []
    anim_clips = anim_plan.get("clips") or []
    anim_keys = anim_plan.get("keys") or []
    if anim_players:
        n = len(anim_players)
        p("const int _AnimPlayer_count = %d;" % n)
        p("int _AnimPlayer_playing[%d] = { %s };" % (
            n, ", ".join(str(int(pl["playing"])) for pl in anim_players)))
        p("float _AnimPlayer_time[%d] = { %s };" % (
            n, ", ".join("0.f" for _ in anim_players)))
        p("const float _AnimPlayer_speed[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(pl["speed"]))
                         for pl in anim_players)))
        p("const int _AnimPlayer_loop[%d] = { %s };" % (
            n, ", ".join(str(int(pl["loop"])) for pl in anim_players)))
        p("const int _AnimPlayer_clip[%d] = { %s };" % (
            n, ", ".join(str(int(pl["clip"])) for pl in anim_players)))
        p("const int _AnimPlayer_owner_class[%d] = { %s };" % (
            n, ", ".join(str(int(pl["owner_class_id"]))
                         for pl in anim_players)))
        p("const int _AnimPlayer_owner_inst[%d] = { %s };" % (
            n, ", ".join(str(int(pl["owner_inst"])) for pl in anim_players)))
        p("const float _AnimPlayer_rest_x[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(pl["rest_x"]))
                         for pl in anim_players)))
        p("const float _AnimPlayer_rest_y[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(pl["rest_y"]))
                         for pl in anim_players)))
        p("const float _AnimPlayer_rest_z[%d] = { %s };" % (
            n, ", ".join("%sf" % repr(float(pl["rest_z"]))
                         for pl in anim_players)))
        nc = len(anim_clips)
        p("const int _AnimClip_count = %d;" % nc)
        p("const float _AnimClip_length[%d] = { %s };" % (
            nc, ", ".join("%sf" % repr(float(c["length"]))
                          for c in anim_clips)))
        p("const int _AnimClip_key_begin[%d] = { %s };" % (
            nc, ", ".join(str(int(c["key_begin"])) for c in anim_clips)))
        p("const int _AnimClip_key_count[%d] = { %s };" % (
            nc, ", ".join(str(int(c["key_count"])) for c in anim_clips)))
        nk = max(1, len(anim_keys))
        keys = anim_keys or [{"t": 0.0, "x": 0.0, "y": 0.0, "z": 0.0}]
        p("const int _AnimKey_count = %d;" % len(keys))
        p("const float _AnimKey_t[%d] = { %s };" % (
            len(keys),
            ", ".join("%sf" % repr(float(k["t"])) for k in keys)))
        p("const float _AnimKey_x[%d] = { %s };" % (
            len(keys),
            ", ".join("%sf" % repr(float(k["x"])) for k in keys)))
        p("const float _AnimKey_y[%d] = { %s };" % (
            len(keys),
            ", ".join("%sf" % repr(float(k["y"])) for k in keys)))
        p("const float _AnimKey_z[%d] = { %s };" % (
            len(keys),
            ", ".join("%sf" % repr(float(k["z"])) for k in keys)))
        anim_skeys = anim_plan.get("sprite_keys") or []
        anim_sbinds = anim_plan.get("sprite_binds") or []
        if anim_skeys or anim_sbinds:
            p("const int _AnimPlayer_sprite_bind_begin[%d] = { %s };" % (
                n, ", ".join(str(int(pl.get("sprite_bind_begin") or 0))
                             for pl in anim_players)))
            p("const int _AnimPlayer_sprite_bind_count[%d] = { %s };" % (
                n, ", ".join(str(int(pl.get("sprite_bind_count") or 0))
                             for pl in anim_players)))
            binds = anim_sbinds or [{
                "target_class_id": 0, "target_inst": 0,
                "key_begin": 0, "key_count": 0,
            }]
            p("const int _AnimSpriteBind_count = %d;" % len(binds))
            p("const int _AnimSpriteBind_target_class[%d] = { %s };" % (
                len(binds),
                ", ".join(str(int(b["target_class_id"])) for b in binds)))
            p("const int _AnimSpriteBind_target_inst[%d] = { %s };" % (
                len(binds),
                ", ".join(str(int(b["target_inst"])) for b in binds)))
            p("const int _AnimSpriteBind_key_begin[%d] = { %s };" % (
                len(binds),
                ", ".join(str(int(b["key_begin"])) for b in binds)))
            p("const int _AnimSpriteBind_key_count[%d] = { %s };" % (
                len(binds),
                ", ".join(str(int(b["key_count"])) for b in binds)))
            skeys = anim_skeys or [{
                "t": 0.0, "tex": 0, "hw": 0.5, "hh": 0.5,
            }]
            p("const int _AnimSpriteKey_count = %d;" % len(skeys))
            p("const float _AnimSpriteKey_t[%d] = { %s };" % (
                len(skeys),
                ", ".join("%sf" % repr(float(k["t"])) for k in skeys)))
            p("const int _AnimSpriteKey_tex[%d] = { %s };" % (
                len(skeys),
                ", ".join(str(int(k["tex"])) for k in skeys)))
            p("const float _AnimSpriteKey_hw[%d] = { %s };" % (
                len(skeys),
                ", ".join("%sf" % repr(float(k["hw"])) for k in skeys)))
            p("const float _AnimSpriteKey_hh[%d] = { %s };" % (
                len(skeys),
                ", ".join("%sf" % repr(float(k["hh"])) for k in skeys)))
    # Mutable SpriteRenderer draw state for m_Sprite PPtr targets.
    for cname in sorted(plan.get("sprite_draw_mutable") or []):
        cl = plan["classes"].get(cname)
        if not cl:
            continue
        idn = _c_ident(cname)
        n = max(1, cl["n"])
        texs, hws, hhs = [], [], []
        for o in cl["instances"]:
            sp = o.get("sprite") or {}
            texs.append(int(sp.get("tex_id") or 0))
            hws.append(float(sp.get("half_w") or 0.5))
            hhs.append(float(sp.get("half_h") or 0.5))
        while len(texs) < n:
            texs.append(0)
            hws.append(0.5)
            hhs.append(0.5)
        p("int _%s_draw_tex[%d] = { %s };" % (
            idn, n, ", ".join(str(t) for t in texs)))
        p("float _%s_draw_hw[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in hws)))
        p("float _%s_draw_hh[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in hhs)))
    # Live localScale for SetWorldScale targets.
    for cname in sorted(plan.get("live_scale_classes") or []):
        cl = plan["classes"].get(cname)
        if not cl:
            continue
        idn = _c_ident(cname)
        n = max(1, cl["n"])
        sxs, sys = [], []
        for o in cl["instances"]:
            ls = o.get("local_scale") or (1.0, 1.0, 1.0)
            sxs.append(float(ls[0]))
            sys.append(float(ls[1]))
        while len(sxs) < n:
            sxs.append(1.0)
            sys.append(1.0)
        p("float _%s_scale_x[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in sxs)))
        p("float _%s_scale_y[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in sys)))
    # Live localRotation for Transform.Rotate / LookAt / eulerAngles / rotation.
    for cname in sorted(plan.get("live_rot_classes") or []):
        cl = plan["classes"].get(cname)
        if not cl:
            continue
        idn = _c_ident(cname)
        n = max(1, cl["n"])
        rxs, rys, rzs, rws = [], [], [], []
        m00s, m01s, m10s, m11s = [], [], [], []
        for o in cl["instances"]:
            lr = o.get("local_rot") or o.get("rot") or (0.0, 0.0, 0.0, 1.0)
            qx, qy, qz, qw = (float(lr[0]), float(lr[1]),
                              float(lr[2]), float(lr[3]))
            rxs.append(qx)
            rys.append(qy)
            rzs.append(qz)
            rws.append(qw)
            m00, m01, m10, m11 = _quat_xy_basis(qx, qy, qz, qw)
            m00s.append(m00)
            m01s.append(m01)
            m10s.append(m10)
            m11s.append(m11)
        while len(rxs) < n:
            rxs.append(0.0)
            rys.append(0.0)
            rzs.append(0.0)
            rws.append(1.0)
            m00s.append(1.0)
            m01s.append(0.0)
            m10s.append(0.0)
            m11s.append(1.0)
        p("float _%s_rot_x[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in rxs)))
        p("float _%s_rot_y[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in rys)))
        p("float _%s_rot_z[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in rzs)))
        p("float _%s_rot_w[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in rws)))
        p("float _%s_rot_m00[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in m00s)))
        p("float _%s_rot_m01[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in m01s)))
        p("float _%s_rot_m10[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in m10s)))
        p("float _%s_rot_m11[%d] = { %s };" % (
            idn, n, ", ".join("%sf" % repr(v) for v in m11s)))
    # Transform field targets (graphicsTrs → Graphics, etc.).
    for (oc, fname), row in sorted(
            (plan.get("transform_field_targets") or {}).items()):
        if oc not in plan["classes"]:
            continue
        idn = _c_ident(oc)
        n = max(1, len(row) or plan["classes"][oc]["n"])
        classes, insts = [], []
        for hit in row:
            if hit:
                classes.append(int(hit[0]))
                insts.append(int(hit[1]))
            else:
                classes.append(-1)
                insts.append(0)
        while len(classes) < n:
            classes.append(-1)
            insts.append(0)
        p("const int _%s_%s_target_class[%d] = { %s };" % (
            idn, fname, n, ", ".join(str(c) for c in classes)))
        p("const int _%s_%s_target_inst[%d] = { %s };" % (
            idn, fname, n, ", ".join(str(c) for c in insts)))
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
        mb_budget = int(add_budget.get(cname) or 0)
        cap = max(1, cl["n"] + mb_budget)
        if mb_budget:
            p("int _%s_inst_count = %d;" % (idn, cl["n"]))
        else:
            p("const int _%s_inst_count = %d;" % (idn, cl["n"]))
        if cl.get("soa_dims"):
            dims = cl["soa_dims"]
            logical = _soa_axis_count(cl)
            p("float _%s_pos[%d][%d] = {" % (idn, cap, dims))
            for ii, o in enumerate(cl["instances"]):
                sx, sy, sz = _instance_storage_pos(o)
                coords = [sx, sy]
                if logical >= 3:
                    coords.append(sz)
                elif dims >= 3:
                    coords.append(0.0)  # 2D → vec4: z = 0
                if dims == 4:
                    coords.append(float(ii))  # .w = instance index
                while len(coords) < dims:
                    coords.append(0.0)
                parts = ["%sf" % repr(v) for v in coords]
                p("    { %s }, /* %s */" % (", ".join(parts), o["name"]))
            for _pad in range(mb_budget):
                parts = ["0.f"] * dims
                p("    { %s }, /* addcomponent spare */" % (", ".join(parts)))
            p("};")
            p("")
        p("%s _%s_inst_array[%d] = {" % (idn, idn, cap))
        for o in cl["instances"]:
            parts = []
            sx, sy, sz = _instance_storage_pos(o)
            for name, ty, bits, kind in cl["members"]:
                if name == "pos_x":
                    parts.append(_init_num(sx, kind))
                elif name == "pos_y":
                    parts.append(_init_num(sy, kind))
                elif name == "pos_z":
                    parts.append(_init_num(sz, kind))
                elif name in o["fields"]:
                    parts.append(_init_num(o["fields"][name], kind))
                elif kind == "idx:Rigidbody2D":
                    parts.append(str(_rb_field_init_index(
                        plan, o, name, "2d")))
                elif kind == "idx:Rigidbody":
                    parts.append(str(_rb_field_init_index(
                        plan, o, name, "3d")))
                elif kind == "idx:AudioSource":
                    parts.append(str(_audiosource_field_init_index(
                        plan, o, name)))
                else:
                    dflt = _member_init_default(cl, name)
                    if dflt is not None:
                        parts.append(_init_num(dflt, kind))
                    else:
                        parts.append("0")
            if not parts:
                parts = ["0"]
            p("    { %s }, /* %s */" % (", ".join(parts), o["name"]))
        for _pad in range(mb_budget):
            parts = []
            for name, ty, bits, kind in cl["members"]:
                parts.append(_init_num(0, kind) if kind in ("f16", "f32")
                             else "0")
            if not parts:
                parts = ["0"]
            p("    { %s }, /* addcomponent spare */" % (", ".join(parts)))
        p("};")
        p("")
    return "\n".join(lines) + "\n"


def _rb_field_init_index(plan, o, fname, kind):
    """Serialized Rigidbody(2D) field → packed table index (-1 if missing)."""
    refs = o.get("object_refs") or {}
    fid = refs.get(fname)
    if kind == "2d":
        by_fid = plan.get("rb2d_by_file_id") or {}
        by_go = plan.get("go_rigidbody2d") or {}
    else:
        by_fid = plan.get("rb3d_by_file_id") or {}
        by_go = plan.get("go_rigidbody") or {}
    if fid is not None and str(fid) != "0" and str(fid) in by_fid:
        return int(by_fid[str(fid)])
    # Same-GO self ref when YAML omitted the PPtr target.
    n = o.get("name") or "obj"
    if n in by_go:
        return int(by_go[n])
    return -1


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


def _init_num(v, kind):
    if kind == "f16":
        return "%du" % _f16_bits(float(v))
    if kind == "f32":
        return "%sf" % (repr(float(v)))
    return str(int(v))


def emit_makefile(outdir):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    py = sys.executable
    # Absolute paths so `make crust-check` works from the outdir.
    return (
        "# generated — engine at -O3 -fno-math-errno, data at -O0; "
        "main.c is a headless host\n"
        "# CC=clang for clang builds; make vectorize-report for loop/SLP miss remarks\n"
        "CC ?= gcc\n"
        "CLANG ?= clang\n"
        "CFLAGS_ENGINE ?= -O3 -fno-math-errno\n"
        "CRUST_ROOT ?= %s\n"
        "CRUST_PY ?= %s\n"
        "CRUST = $(CRUST_PY) -m shivyc.main --no-cache\n"
        ".PHONY: all crust-check vectorize-report clean\n"
        "all: game\n"
        "engine.o: engine.c\n"
        "\t$(CC) $(CFLAGS_ENGINE) -c -o $@ $<\n"
        "data.o: data.c\n"
        "\t$(CC) -O0 -c -o $@ $<\n"
        "main.o: main.c engine_draw.h\n"
        "\t$(CC) -O2 -c -o $@ $<\n"
        "game: engine.o data.o main.o\n"
        "\t$(CC) -O2 -o $@ engine.o data.o main.o -lm\n"
        "# Clang remarks: which loops miss auto-vectorization (stderr).\n"
        "vectorize-report: engine.c\n"
        "\t$(CLANG) $(CFLAGS_ENGINE) "
        "-Rpass-missed=loop-vectorize,slp-vectorize "
        "-c -o engine.vectorize.o engine.c\n"
        "# Compile packed C with crust/shivyc (C++ twins gate at pack time).\n"
        "crust-check: engine.c data.c main.c engine.cpp data.cpp main.cpp\n"
        "\tcd $(CRUST_ROOT) && $(CRUST) -c -D CRUST_NO_POSIX_MKDIR "
        "-o $(CURDIR)/engine.crust.o $(CURDIR)/engine.c\n"
        "\tcd $(CRUST_ROOT) && $(CRUST) -c -D CRUST_NO_POSIX_MKDIR "
        "-o $(CURDIR)/data.crust.o $(CURDIR)/data.c\n"
        "\tcd $(CRUST_ROOT) && $(CRUST) -c "
        "-o $(CURDIR)/main.crust.o $(CURDIR)/main.c\n"
        "clean:\n"
        "\trm -f engine.o data.o main.o game engine.vectorize.o "
        "engine.crust.o data.crust.o main.crust.o\n"
        % (repo, py)
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
        "    for (i = 0; i < 60; i = i + 1) {\n"
        "        engine_tick();\n"
        "        if (engine_wants_quit()) break;\n"
        "    }\n"
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
    _progress("scanning %s" % root)
    _progress("reading .meta guid maps")
    assets = _asset_guid_map(root)
    guids = _guid_map(root, asset_guids=assets)
    objects = []
    lights = []
    cameras = []
    hierarchy = []
    _progress("finding .unity scenes")
    scenes = list(_walk_files(root, (".unity",)))
    _progress("parsing %d .unity scene(s)" % len(scenes))
    for si, path in enumerate(scenes):
        _progress("  scene %d/%d %s" % (
            si + 1, len(scenes), os.path.basename(path)))
        objs, scene_lights, scene_cams, scene_hier = parse_unity_yaml(
            _read(path), guid_to_script=guids, asset_guids=assets)
        objects.extend(objs)
        lights.extend(scene_lights)
        cameras.extend(scene_cams)
        hierarchy.extend(scene_hier)
    for path in _walk_files(root, (".tscn",)):
        objects.extend(parse_godot_tscn(_read(path)))
    for path in _walk_files(root, (".json",)):
        if os.path.basename(path) == "blender_pack.json":
            objects.extend(parse_blender_json(_read(path)))
    sorting_layers = _load_sorting_layers(root)

    # Only analyze MonoBehaviour scripts authored on scene objects — vendor
    # helpers (e.g. CwHelper) must not refuse the pack via unused usings.
    scene_scripts = set()
    for o in objects:
        sp = o.get("script")
        if sp:
            scene_scripts.add(os.path.abspath(sp))
    scripts = [p for p in _walk_files(root, (".cs",))
               if _is_player_csharp(root, p)]
    if scene_scripts:
        scripts = [p for p in scripts
                   if os.path.abspath(p) in scene_scripts]
    _progress("analyzing %d script(s)" % len(scripts))
    analyses = []
    for i, p in enumerate(scripts):
        if scripts and ((i + 1) % 25 == 0 or i + 1 == len(scripts)):
            _progress("  scripts %d/%d" % (i + 1, len(scripts)))
        analyses.append(analyze_script(p))
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
    _progress("scene objects=%d lights=%d cameras=%d hierarchy=%d" % (
        len(objects), len(lights), len(cameras), len(hierarchy)))
    sw, sh = player_screen(root)
    _bake_ui_images(objects, cameras, sw, sh, asset_guids=assets)
    objects = [o for o in objects if not o.get("ui_scaffold")]
    _apply_sprite_sorting(objects, sorting_layers)
    _attach_sprite_textures(objects, assets)
    return objects, analyses, lights, cameras, hierarchy


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
    """Gate generated C through cpprust, then compile the result with crust.

    unity_pack lowers by hand; this proves the result still sits inside the
    crust subset that `tools/cpprust.py` accepts — `_check_unsupported` plus
    a full `translate` pass (the csrust C++ half). The translated C is then
    compiled with `shivyc` so pack fails if crust cannot build it. Raises
    PackError on subset violations or crust compile failure.
    """
    import tools.cpprust as cpprust
    try:
        scan = cpprust._blank_directives(cpprust._strip_comments(text))
        cpprust._check_unsupported(scan, path)
        translated = cpprust.translate(text, path=path)
    except cpprust.CppError as e:
        raise PackError(
            "emitted %s left the crust / cpprust subset: %s"
            % (path, e.message))
    _crust_compile_c(translated, path)
    return translated


def _crust_compile_c(text, path, defines=None):
    """Compile *text* with shivyc/crust; raise PackError on failure."""
    import shutil
    import subprocess
    import tempfile
    defines = list(defines or ())
    # Player.log mkdir needs errno/sys/stat — crust's include subset has
    # neither, so always gate those blocks when compiling through shivyc.
    if "CRUST_NO_POSIX_MKDIR" not in defines:
        defines.append("CRUST_NO_POSIX_MKDIR")
    tmpdir = tempfile.mkdtemp(prefix="upack-crust-")
    src = os.path.join(tmpdir, "tu.c")
    obj = os.path.join(tmpdir, "tu.o")
    try:
        with open(src, "w") as f:
            f.write(text)
        if '#include "engine_draw.h"' in text:
            with open(os.path.join(tmpdir, "engine_draw.h"), "w") as f:
                f.write(emit_engine_draw_h())
        cmd = [sys.executable, "-m", "shivyc.main", "--no-cache", "-c",
               "-I", tmpdir]
        for d in defines:
            cmd.extend(["-D", d])
        cmd.extend(["-o", obj, src])
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=repo)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise PackError(
                "emitted %s failed crust/shivyc compile: %s"
                % (path, err or ("exit %d" % r.returncode)))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _refused_api_site(analyses, api):
    """First (path, text, idx) for a refused API token, or None."""
    patterns = {
        "Canvas": (
            r"AddComponent\s*<\s*(?:UnityEngine\.)?(Canvas)\s*>|"
            r"typeof\s*\(\s*(Canvas)\s*\)|"
            r"Canvas\.(ForceUpdateCanvases)\b"
        ),
        "InputAction": r"(?<![\w.])InputAction\b",
        "ParticleSystem.Emit": r"ParticleSystem\.(Emit)\b",
        "AnimationCurve.Evaluate": r"AnimationCurve\.(Evaluate)\b",
        "Gamepad.current": r"(?<![\w.])Gamepad\.(current)\b",
        "Keyboard": r"(?<![\w.])Keyboard\b",
        "Console": r"(?<![\w.])Console\b",
    }
    pat = patterns.get(api)
    if not pat:
        return None
    for a in analyses:
        path = a.get("path") or ""
        text = None
        for c in a.get("classes") or []:
            if c.get("file_text") is not None:
                text = c["file_text"]
                break
        if text is None and path and os.path.isfile(path):
            text = _read(path)
        if not text:
            continue
        scan = cs2cpp._blank(text)
        m = re.search(pat, scan)
        if m:
            idx = m.start()
            for g in range(1, (m.lastindex or 0) + 1):
                if m.start(g) >= 0:
                    idx = m.start(g)
                    break
            return path, text, idx
    return None


def pack(root, outdir, soa=False, soa_vec4=False):
    objects, analyses, lights, cameras, hierarchy = load_project(root)
    used_apis = set()
    for a in analyses:
        used_apis |= a["apis"]
    for api, reason in sorted(_REFUSED_API.items()):
        if api not in used_apis:
            continue
        site = _refused_api_site(analyses, api)
        if site:
            path, text, idx = site
            if "." in api and api != "UnityEngine.UI":
                ty, member = api.split(".", 1)
                _raise_cs(
                    path, text, idx, "CS0117",
                    "'%s' does not contain a definition for '%s'"
                    % (ty, member))
            else:
                _raise_cs(path, text, idx, "CS0246", _CS0246 % (
                    api if api != "Canvas" else "Canvas"))
        # No precise site — still Unity-shaped.
        if "." in api and api != "UnityEngine.UI":
            ty, member = api.split(".", 1)
            raise PackError(
                "<cs>(1,1): error CS0117: '%s' does not contain a "
                "definition for '%s'" % (ty, member))
        raise PackError(
            "<cs>(1,1): error CS0246: %s" % (_CS0246 % api))
    add_types = _collect_addcomponent_types(analyses)
    if "Camera.main" in used_apis and not cameras:
        site = _refused_api_site(analyses, "Camera.main")
        if site:
            path, text, idx = site
            _raise_cs(
                path, text, idx, "CS0117",
                "'Camera' does not contain a definition for 'main'")
        raise PackError(
            "<cs>(1,1): error CS0117: 'Camera' does not contain a "
            "definition for 'main'")
    _progress("planning layouts (%d objects)" % len(objects))
    plan = plan_layouts(objects, analyses)
    if soa or soa_vec4:
        plan = apply_soa_layout(plan, vec4=bool(soa_vec4))
    else:
        plan = dict(plan)
        plan["soa"] = False
        plan["soa_vec4"] = False
    _validate_addcomponent_types(add_types, plan, analyses)
    gc_types = set()
    for a in analyses:
        gc_types |= set(a.get("getcomponent_types") or [])
    _validate_getcomponent_types(gc_types, plan, analyses)
    plan["addcomponent_types"] = sorted(add_types)
    plan["addcomponent_budget"] = _addcomponent_budget(analyses, plan)
    plan["disallow_multiple_types"] = sorted(_disallow_multiple_types(analyses))
    plan["go_has_sprite"] = sorted(_gos_with_sprite(plan))
    plan["lights"] = list(lights)
    plan["light_count"] = len(lights)
    plan["scene_hierarchy"] = list(hierarchy)
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
    _ensure_texture_guids(
        plan["textures"], _anim_sprite_guids(objects),
        _asset_guid_map(root))
    kb_keys = set()
    for a in analyses:
        kb_keys |= set(a.get("keyboard_keys") or [])
    plan["keyboard_keys"] = sorted(kb_keys)
    company, product = player_identity(root)
    plan["company_name"] = company
    plan["product_name"] = product
    plan["project_root"] = os.path.abspath(root)
    plan["data_path"] = os.path.join(os.path.abspath(root), "Assets")
    plan["persistent_data_path"] = unity_persistent_data_path(company, product)
    sw, sh, sfs, snative, smax = player_display(root)
    plan["screen_width"] = sw
    plan["screen_height"] = sh
    plan["screen_fullscreen"] = sfs
    plan["screen_fullscreen_native"] = snative
    plan["screen_maximized"] = smax
    go_names, go_comps = _build_go_tables(plan)
    ui_gc = set()
    for a in analyses:
        ui_gc |= set(a.get("getcomponent_types") or [])
    ui_gc &= _UI_GETCOMPONENT_TYPES
    if ("transform.Find" in used_apis or "transform.parent" in used_apis
            or "transform.SetParent" in used_apis
            or "transform.GetSiblingIndex" in used_apis
            or ui_gc):
        go_names, go_comps = _extend_go_tables_for_find(
            plan, go_names, go_comps)
    plan["go_names"] = go_names
    plan["go_components"] = go_comps
    plan["go_ui_components"] = _build_go_ui_component_maps(plan)
    plan["go_parents"] = _build_go_parents(plan)
    plan["go_siblings"] = _build_go_sibling_indices(plan["go_parents"])
    plan["ui_buttons"] = _build_ui_buttons(plan)
    rb2d, rb3d, go_rb2d, go_rb3d, rb2d_by_fid, rb3d_by_fid = (
        _build_rigidbody_tables(plan))
    plan["rigidbody2d"] = rb2d
    plan["rigidbody"] = rb3d
    plan["go_rigidbody2d"] = go_rb2d
    plan["go_rigidbody"] = go_rb3d
    plan["rb2d_by_file_id"] = rb2d_by_fid
    plan["rb3d_by_file_id"] = rb3d_by_fid
    asrc, go_as, as_by_fid, clip_guids = _build_audiosource_tables(plan)
    plan["audiosources"] = asrc
    plan["go_audiosource"] = go_as
    plan["audiosource_by_file_id"] = as_by_fid
    plan["audioclip_guids"] = clip_guids
    _attach_transform_parents(plan)
    if "transform.SetParent" in used_apis:
        plan["has_transform_parents"] = True
    plan["collider2d"] = _build_collider2d_tables(plan)
    plan["collider3d"] = _build_collider3d_tables(plan)
    plan["animation"] = _build_animation_tables(plan)
    _resolve_transform_field_targets(plan)
    # Transform.TransformPoint / matrices need live localScale (seeded authored).
    live_scale = set(plan.get("live_scale_classes") or [])
    live_scale |= set(plan.get("transform_point_classes") or [])
    live_scale |= set(plan.get("transform_matrix_classes") or [])
    plan["live_scale_classes"] = sorted(live_scale)
    os.makedirs(outdir, exist_ok=True)
    _progress("emitting engine.c (%d classes)" % len(plan["classes"]))
    engine = emit_engine(plan, analyses, used_apis)
    _progress("emitting data.c (%d texture(s))" % len(plan.get("textures") or []))
    data = emit_data(plan, used_apis)
    main_c = emit_main()
    # C++-subset twins: same text, fed through cpprust then crust (csrust pipe).
    engine_cpp = (
        "/* generated by tools/unity_pack.py — C++ subset for cpprust */\n"
        + (engine.split("\n", 1)[1] if engine.startswith("/*") else engine))
    data_cpp = (
        "/* generated by tools/unity_pack.py — C++ subset for cpprust */\n"
        + (data.split("\n", 1)[1] if data.startswith("/*") else data))
    main_cpp = (
        "/* generated by tools/unity_pack.py — C++ subset for cpprust */\n"
        + (main_c.split("\n", 1)[1] if main_c.startswith("/*") else main_c))
    _progress("validating engine.c through cpprust + crust")
    validate_emitted_c(engine_cpp, "engine.cpp")
    _progress("validating data.c through cpprust + crust")
    validate_emitted_c(data_cpp, "data.cpp")
    _progress("validating main.c through cpprust + crust")
    validate_emitted_c(main_cpp, "main.cpp")
    _progress("writing %s" % outdir)
    with open(os.path.join(outdir, "engine.c"), "w") as f:
        f.write(engine)
    with open(os.path.join(outdir, "data.c"), "w") as f:
        f.write(data)
    with open(os.path.join(outdir, "main.c"), "w") as f:
        f.write(main_c)
    with open(os.path.join(outdir, "engine.cpp"), "w") as f:
        f.write(engine_cpp)
    with open(os.path.join(outdir, "data.cpp"), "w") as f:
        f.write(data_cpp)
    with open(os.path.join(outdir, "main.cpp"), "w") as f:
        f.write(main_cpp)
    with open(os.path.join(outdir, "engine_draw.h"), "w") as f:
        f.write(emit_engine_draw_h())
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
    _progress("done")
    return plan


def project_folder_name(root):
    """Unity project folder name (not productName)."""
    return os.path.basename(os.path.abspath(root).rstrip(os.sep)) or "Player"


def exe_filename(product):
    """Player binary name: productName plus the platform executable suffix."""
    name = (product or "Player").strip() or "Player"
    name = name.replace("/", "_").replace("\\", "_").replace("\0", "_")
    if sys.platform.startswith("win"):
        for ch in '<>:"|?*':
            name = name.replace(ch, "_")
        if not name.lower().endswith(".exe"):
            name += ".exe"
    return name


def default_pack_dir(root):
    """$TMPDIR/<project folder> — default place for sources and the player."""
    import tempfile
    return os.path.join(tempfile.gettempdir(), project_folder_name(root))


def build_player_executable(outdir, product):
    """Compile engine.c + data.c and link a player named after productName.

    Prefers examples/unity_pack/gles2_window.c when pkg-config finds glfw3.
    Otherwise links the generated headless main.c.
    """
    import subprocess
    cc = os.environ.get("CC") or "gcc"
    exe = os.path.join(outdir, exe_filename(product))
    engine_c = os.path.join(outdir, "engine.c")
    data_c = os.path.join(outdir, "data.c")
    engine_o = os.path.join(outdir, "engine.o")
    data_o = os.path.join(outdir, "data.o")

    def _run(cmd):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise PackError(
                "player build failed (%s): %s" % (
                    " ".join(cmd[:6]), err or ("exit %d" % r.returncode)))

    _progress("compiling engine.c")
    _run([cc, "-O3", "-fno-math-errno", "-c", "-o", engine_o, engine_c])
    _progress("compiling data.c")
    _run([cc, "-O0", "-c", "-o", data_o, data_c])

    host = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "examples", "unity_pack", "gles2_window.c"))
    use_window = False
    cflags = []
    libs = []
    if os.path.isfile(host):
        try:
            chk = subprocess.run(
                ["pkg-config", "--exists", "glfw3"],
                capture_output=True)
            if chk.returncode == 0:
                cflags = subprocess.check_output(
                    ["pkg-config", "--cflags", "glfw3"], text=True).split()
                libs = subprocess.check_output(
                    ["pkg-config", "--libs", "glfw3"], text=True).split()
                use_window = True
        except (OSError, subprocess.CalledProcessError):
            use_window = False
    if use_window:
        _progress("linking window player %s" % exe)
        _run([cc, "-O2", "-o", exe, host, engine_o, data_o,
              "-I", outdir] + cflags + libs + ["-lGLESv2", "-lm"])
    else:
        main_o = os.path.join(outdir, "main.o")
        _progress("linking headless player %s" % exe)
        _run([cc, "-O2", "-c", "-o", main_o,
              os.path.join(outdir, "main.c")])
        _run([cc, "-O2", "-o", exe, engine_o, data_o, main_o, "-lm"])
    return exe


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
    if len(args) != 1:
        sys.stderr.write(
            "usage: unity_pack.py <project-dir> [-o <out-dir>] "
            "[--soa | --soa-vec4]\n"
            "  default out-dir: $TMPDIR/<project folder>\n"
            "  player binary:   <productName>  (Windows: <productName>.exe)\n")
        return 2
    if outdir is None:
        outdir = default_pack_dir(args[0])
    try:
        plan = pack(args[0], outdir, soa=soa, soa_vec4=soa_vec4)
        exe = build_player_executable(
            outdir, plan.get("product_name") or "Player")
    except PackError as e:
        # csc/Unity diagnostics print verbatim; other refusals keep the prefix.
        if ": error CS" in e.message:
            sys.stderr.write("%s\n" % e.message)
        else:
            sys.stderr.write("unity_pack: %s\n" % e.message)
        return 1
    sys.stderr.write(
        "unity_pack: %d classes, 2d=%s, soa=%s, soa_vec4=%s, "
        "wrote %s/{engine.c,data.c,main.c,engine.cpp,data.cpp,main.cpp,"
        "engine_draw.h}\n"
        "unity_pack: executable %s\n"
        % (len(plan["classes"]), plan["two_d"], plan.get("soa"),
           plan.get("soa_vec4"), outdir, exe))
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
