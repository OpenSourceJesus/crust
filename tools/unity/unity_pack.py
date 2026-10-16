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

    python3 tools/unity/unity_pack.py <project>
    python3 tools/unity/unity_pack.py <project> -o <outdir>
    python3 tools/unity/unity_pack.py <project> -o <outdir> --force

Default output is $TMPDIR/<project-folder>/<productName>[.exe], a linked
player (GLFW window when glfw3 is present, otherwise the headless host).
Unchanged projects reuse ``outdir/.unity_pack_stamp.json`` (skip emit /
transpile). Script-only edits reuse ``.unity_pack_scene_cache`` (skip meta /
scene / sprite reload). ``--force`` always rebuilds.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import pickle
import re
import sys
import math
import copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import tools.cs2cpp as cs2cpp
import tools.unity.unity_pack_runtime as runtime  # noqa: E402

# unity_pack is split by subsystem; each module's names are re-exported here.
from tools.unity.unity_pack_common import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_physics import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_sprites import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_ui import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_anim import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_audio import *  # noqa: E402,F401,F403
from tools.unity.unity_pack_build import *  # noqa: E402,F401,F403
import tools.godot_pack as _godot  # noqa: E402
import tools.unity.unity_pack_vectors as _vec  # noqa: E402

# C# helpers that moved to cs2cpp; the old names stay for callers and tests.
_c_string = cs2cpp.c_string
_string_literal_value = cs2cpp.string_literal_value
_match_call_args = cs2cpp.match_call_args
_MODIFIERS = cs2cpp.MODIFIERS
_methods_in = cs2cpp.methods_in
_interface_methods = cs2cpp.interface_methods
_property_names = cs2cpp.property_names
_properties_as_methods = cs2cpp.properties_as_methods
_method_c_arg_names = cs2cpp.method_c_arg_names
_method_arg_type_suffix = cs2cpp.method_arg_type_suffix
_method_c_symbol = cs2cpp.method_c_symbol
_overload_method_names = cs2cpp.overload_method_names
_blank_method_bodies = cs2cpp.blank_method_bodies



def _assets_rel_path(path):
    """Unity-style path: `Assets/...` when under an Assets tree; `res://...`
    in a Godot project."""
    if _godot.GODOT_ROOT[0] and path:
        return _godot.godot_display_path(path)
    norm = path.replace("\\", "/")
    i = norm.find("/Assets/")
    if i >= 0:
        return norm[i + 1:]
    if norm.startswith("Assets/"):
        return norm
    return os.path.basename(path) if path else "<cs>"


def _cs_diag(path, text, idx, code, message, kind="error"):
    """Unity/csc diagnostic: `Assets/.../File.cs(line,col): error CSxxxx: …`.
    cs2cpp.cs_diag with Unity's `Assets/` path display."""
    return cs2cpp.cs_diag(path, text, idx, code, message, kind,
                          display_path=_assets_rel_path)


def _report_stub(plan, site, cl, m, why):
    """A method the translator could not lower: warn at its site, or refuse.

    It used to become an empty function with no word said, which changed
    what the program does -- a `Debug.Log` vanished, and so did whole
    `File` calls when their lowering regressed, with the tests that pinned
    them reporting only that a string was missing. Now it is a csc-style
    diagnostic at the method, naming the C# that was left: a warning by
    default, since the translator does not yet cover everything the packer
    accepts, and an error under `pack(strict=True)` / `--strict`. Every stub
    is also recorded in `plan["stubs"]`.

    CS8000 is csc's "this language feature is not yet implemented", which
    is exactly what a stub is.
    """
    what, text = why
    text = " ".join(str(text).split())
    message = ("`%s.%s` is not lowered yet (`%s`: %s)"
               % (cl["name"], m["name"], text, what.rstrip(".")))
    if not plan.get("strict"):
        # only a warning says what happens next; an error stops the pack
        message += "; it is emitted as an empty method"
    path = site.get("path") or "<cs>"
    ft = site.get("file_text") or ""
    at = int(site.get("body_abs") or 0)
    plan.setdefault("stubs", []).append(
        {"class": cl["name"], "method": m["name"], "path": path,
         "text": text, "what": what})
    if plan.get("strict"):
        if ft:
            raise PackError(_cs_diag(path, ft, at, "CS8000", message))
        raise PackError("%s(1,1): error CS8000: %s"
                        % (_assets_rel_path(path), message))
    if ft:
        diag = _cs_diag(path, ft, at, "CS8000", message, kind="warning")
    else:
        diag = "%s(1,1): warning CS8000: %s" % (_assets_rel_path(path),
                                               message)
    sys.stderr.write(diag + "\n")


def _raise_cs(path, text, idx, code, message):
    raise PackError(_cs_diag(path, text, idx, code, message))


def _raise_cs_at_site(site, body_idx, code, message):
    """CS diagnostic using method-body offset + emit site (path / file_text).
    cs2cpp.cs_diag_at_site with Unity's `Assets/` path display."""
    raise PackError(cs2cpp.cs_diag_at_site(site, body_idx, code, message,
                                           display_path=_assets_rel_path))


def _raise_unknown_component_type(t, analyses, ops=("AddComponent", "GetComponent")):
    """Unknown / refused component type → Unity CS0246 at the type token."""
    analyses = analyses or []
    for a in analyses:
        apath = a.get("path") or ""
        # each class's own text with its own path: the first class's text
        # under the analysis's path reported a use in SpriteExploder.cs at
        # Explodable.cs(188,727), a line that file does not have
        sources = [(c.get("path") or apath, c["file_text"])
                   for c in a.get("classes") or [] if c.get("file_text") is not None]
        if not sources and apath and os.path.isfile(apath):
            sources = [(apath, _read(apath))]
        for path, text in sources:
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


def _mb_bases_from_header(scan, name_end, brace):
    """C# ``class Foo : Bar, IBaz`` → ``[\"Bar\", \"IBaz\"]`` (simple names)."""
    # `class S<T> : Base where T : Base`: neither the type parameters nor
    # the constraint clause name a base
    header = re.sub(r"^\s*<[^:{]*?>", "", scan[name_end:brace])
    header = re.split(r"\bwhere\b", header, maxsplit=1)[0]
    if ":" not in header:
        return []
    clause = header.split(":", 1)[1]
    out = []
    for part in clause.split(","):
        tok = part.strip().split("<")[0].strip()
        tok = tok.split(".")[-1].strip()
        if tok and tok[:1].isupper() and re.match(r"^\w+$", tok):
            out.append(tok)
    return out


def _collect_mb_bases(analyses):
    """Packed/analyzed MB name → immediate base type names."""
    bases = {}
    for a in analyses or []:
        for c in a.get("classes") or []:
            name = c.get("name")
            if not name:
                continue
            b = list(c.get("bases") or [])
            if b:
                bases[name] = b
    return bases


def _empty_instance_lists(plan):
    """{"T.f"}: a `List<U>` / `U[]` field of a MonoBehaviour T where no
    packed class is a U (and, for a static, none is a T). No such object
    exists in the packed game, so nothing that runs can fill the list
    (`_drop_empty_instance_lists`; an instance one through a handle,
    `_handle_field_access`). ponytail: `Add(null)` would fill one."""
    import tools.unity.unity_pack_common as _cm
    # every project script, not only the analyzed ones: a class no scene
    # places is not analyzed
    bases = {k: list(v) for k, v in (plan.get("mb_bases") or {}).items()}
    fields, structs = [], set()
    for t in _cm.SOURCE_ORIGINAL.values():
        scan = cs2cpp._blank(t)
        structs.update(re.findall(r"\bstruct\s+(\w+)", scan))
        opens = []
        for m in re.finditer(r"\bclass\s+(\w+)(?:\s*<[^>{]*>)?\s*(?::\s*([^{]+))?\{",
                             scan):
            bases.setdefault(m.group(1), [
                re.sub(r"<.*", "", b).strip().split(".")[-1]
                for b in (m.group(2) or "").split(",") if b.strip()])
            opens.append((m.end() - 1, m.group(1)))
        for m in re.finditer(
                r"\b(static\s+)?(?:readonly\s+)?(?:List\s*<\s*(\w+)\s*>|(\w+)\s*\[\s*\])"
                r"\s+(\w+)\s*[=;]", scan):
            owner = None
            for at, name in opens:
                # ponytail: the innermost class opened before the field and
                # not yet closed, by brace balance
                if at < m.start() and scan.count("{", at, m.start()) > \
                        scan.count("}", at, m.start()):
                    owner = name
            if owner:
                fields.append((owner, m.group(4), m.group(2) or m.group(3),
                               bool(m.group(1))))
    packed = list(plan.get("classes") or {})

    def absent(t):
        return (_mb_is_a(t, "MonoBehaviour", bases)
                and not any(_mb_is_a(c, t, bases) for c in packed))
    # a packed class's static `U[]` of a project class or struct with no
    # packed rows (not `_static_ref_array`): no storage, so every method
    # reading it is a stub, and a reset is unseen
    plan["storageless_arrays"] = {
        "%s.%s" % (cn, f["name"])
        for cn, c in (plan.get("classes") or {}).items()
        for f in c.get("class_consts") or ()
        if f.get("static") and _array_elem_name(f.get("ty") or "") in (
            set(bases) | structs) and not _static_ref_array(f, plan)}
    # an instance list's owner may be packed: its rows exist, the list's
    # elements never do
    return {"%s.%s" % (o, f) for o, f, u, st in fields
            if absent(u) and (absent(o) or not st)}


def _drop_empty_instance_lists(text, plan):
    """An always-empty list (`_empty_instance_lists`): `.Count` / `.Length`
    is 0, `.Clear()` does nothing, and a `for` counting up from 0 over it
    never runs, so its body (about objects that do not exist) goes."""
    for n in sorted(plan.get("storageless_arrays") or ()):
        o, f = n.split(".")
        text = cs2cpp.code_sub(
            r"(?<![\w.])%s\s*\.\s*%s\s*=(?!=)\s*new\s+\w+\s*\[\s*0\s*\]\s*;" % (
                re.escape(o), re.escape(f)), ";", text)
    names = plan.get("empty_lists") or ()
    if not names:
        return text
    alt = "|".join(r"%s\s*\.\s*%s" % tuple(map(re.escape, n.split(".")))
                   for n in sorted(names))
    text = cs2cpp.code_sub(r"(?<![\w.])(?:%s)\s*\.\s*(?:Count|Length)\b" % alt,
                           "0", text)
    text = cs2cpp.code_sub(r"(?<![\w.])(?:%s)\s*\.\s*Clear\s*\(\s*\)" % alt,
                           "(void)0", text)
    rx = re.compile(r"(?<![\w.])for\s*\(\s*int\s+(\w+)\s*=\s*0\s*;\s*\1\s*<\s*0"
                    r"\s*;[^;{}]*\)\s*\{")
    while True:
        scan = cs2cpp._blank(text)
        m = rx.search(scan)
        if not m:
            return text
        depth, j = 0, m.end() - 1
        for j in range(m.end() - 1, len(scan)):
            depth += {"{": 1, "}": -1}.get(scan[j], 0)
            if depth == 0:
                break
        # (the body's lines stay the C# lines: NRE sites name them)
        text = (text[:m.start()] + ";" + "\n" * text.count("\n", m.start(), j)
                + text[j + 1:])


def _lower_handle_enabled(text, plan):
    """`Other_AT(e).enabled`, a packed script through a handle: read with
    `Other_get_enabled`, written with `Other_set_enabled` (OnEnable /
    OnDisable as Unity runs them). A null handle throws, as `_AT` does."""
    idns = {_c_ident(c) for c in plan.get("classes") or {}}
    scan = cs2cpp._blank(text)
    out, k = [], 0
    for m in re.finditer(r"(?<![\w.])(\w+)_AT\(", scan):
        if m.start() < k or m.group(1) not in idns:
            continue
        close = _match_close(scan, m.end() - 1, "(", ")")
        tail = close is not None and re.match(
            r"\s*\.\s*enabled\b(?:\s*=(?!=)\s*([^;]+);)?", scan[close + 1:])
        if not tail:
            continue
        e = "_engine_nre_ix(%s)" % text[m.end():close]
        rep = ("%s_set_enabled(%s, (int)(%s));" % (
            m.group(1), e, text[close + 1 + tail.start(1):close + 1 + tail.end(1)])
            if tail.group(1) else "%s_get_enabled(%s)" % (m.group(1), e))
        out.append(text[k:m.start()] + rep)
        k = close + 1 + tail.end()
    return "".join(out) + text[k:]


def _mb_is_a(cname, ancestor, bases_map, stack=None):
    """True if *cname* is *ancestor* or inherits it (authored bases)."""
    if cname == ancestor:
        return True
    stack = stack or set()
    if cname in stack:
        return False
    stack.add(cname)
    for b in bases_map.get(cname) or []:
        if _mb_is_a(b, ancestor, bases_map, stack):
            return True
    return False


def _gcic_collector_types(tname, plan, bases_map=None):
    """Packed types whose instances count as GetComponentsInChildren<T>.

    Includes ``T`` when packed, plus every packed subclass of ``T``.
    """
    bases_map = bases_map or plan.get("mb_bases") or {}
    classes = plan.get("classes") or {}
    out = []
    if tname in classes:
        out.append(tname)
    for cname in sorted(classes):
        if cname == tname:
            continue
        if _mb_is_a(cname, tname, bases_map):
            out.append(cname)
    return out


def _analyzed_mb_typenames(analyses):
    """MonoBehaviour / script class names seen in analyses."""
    return {c["name"] for a in (analyses or [])
            for c in a.get("classes") or [] if c.get("name")}


# A call into the C math library anywhere in the emitted engine. The header
# is included by feature (`want_math`, `want_anim`, ..), and this is the
# net under that list: it checked three names, so ParticleSystem's
# simulation -- `cosf` / `sinf` for the emitter's shape -- failed to
# compile in every project that had one, `undeclared identifier 'cosf'`.
_LIBM_CALL = re.compile(
    r"(?<![\w.])(?:sin|cos|tan|asin|acos|atan|atan2|sinh|cosh|tanh|sqrt|cbrt"
    r"|pow|exp|exp2|log|log2|log10|floor|ceil|round|trunc|fmod|fabs|hypot"
    r"|fmin|fmax|copysign)f?\s*\(")

# UnityEngine.ParticleSystem's instance methods: called through the type
# they are CS0120, not CS0117 -- the member exists, it needs an object.
_PARTICLE_SYSTEM_INSTANCE_MEMBERS = frozenset((
    "Emit", "Play", "Stop", "Pause", "Clear", "Simulate", "IsAlive",
    "GetParticles", "SetParticles", "TriggerSubEmitter", "GetComponent",
))
# `ParticleSystem ParticleSystem` / `ParticleSystem` as a parameter name: the
# name then means the variable.
_PS_NAMED_VARIABLE = re.compile(
    r"(?<![\w.])[A-Za-z_][\w.<>]*\s+ParticleSystem\s*[;=,)]")

# System.IO.File members we emit. Others → CS0117 (File is in scope via using).
_FILE_SUPPORTED = frozenset({
    "WriteAllText", "AppendAllText", "WriteAllBytes", "ReadAllBytes",
    "Exists", "Delete", "CreateText", "OpenText", "Copy",
    # tools/unity/unity_pack_runtime.py
    "ReadAllText", "ReadAllLines",
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
    "Translate",
    "localScale", "parent", "gameObject", "SetParent", "GetSiblingIndex",
    "worldToLocalMatrix", "localToWorldMatrix",
    "localPosition", "localRotation",
    "TransformPoint",
    # RectTransform members (UI Transform is a RectTransform).
    "anchoredPosition", "sizeDelta",
})


# Every member UnityEngine.Transform has (and what it inherits from
# Component / Object). A real one this pack does not lower is "not packed
# yet"; CS1061 -- "does not contain a definition" -- is for a member Unity
# does not have. Every unlowered member used to get CS1061, telling the user
# that `Translate`, `right` or `childCount` do not exist.
_TRANSFORM_UNITY_MEMBERS = frozenset({
    "position", "localPosition", "eulerAngles", "localEulerAngles", "right",
    "up", "forward", "rotation", "localRotation", "localScale", "parent",
    "worldToLocalMatrix", "localToWorldMatrix", "root", "childCount",
    "lossyScale", "hasChanged", "hierarchyCapacity", "hierarchyCount",
    "gameObject", "tag", "name", "transform", "hideFlags",
    "SetParent", "SetPositionAndRotation", "SetLocalPositionAndRotation",
    "Translate", "Rotate", "RotateAround", "LookAt", "TransformDirection",
    "InverseTransformDirection", "TransformVector", "InverseTransformVector",
    "TransformPoint", "InverseTransformPoint", "DetachChildren",
    "SetAsFirstSibling", "SetAsLastSibling", "SetSiblingIndex",
    "GetSiblingIndex", "Find", "IsChildOf", "GetChild", "GetEnumerator",
    "GetComponent", "GetComponents", "GetComponentInChildren",
    "GetComponentsInChildren", "GetComponentInParent", "GetComponentsInParent",
    "TryGetComponent", "CompareTag", "SendMessage", "SendMessageUpwards",
    "BroadcastMessage", "GetInstanceID", "ToString", "Equals", "GetHashCode",
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
        if member in _TRANSFORM_UNITY_MEMBERS:
            _raise_cs(path, text, member_idx, "CS8000",
                      "'Transform.%s' is not packed yet" % member)
        _raise_cs(
            path, text, member_idx, "CS1061",
            "'Transform' does not contain a definition for '%s' and no "
            "accessible extension method '%s' accepting a first argument of "
            "type 'Transform' could be found (are you missing a using "
            "directive or an assembly reference?)"
            % (member, member))
    for m in re.finditer(
            r"(?<![.\w])(?:this\s*\.\s*)?rectTransform\s*\.\s*(\w+)", scan):
        member = m.group(1)
        if member in _RECTTRANSFORM_SUPPORTED:
            continue
        member_idx = m.start(1)
        _raise_cs(
            path, text, member_idx, "CS1061",
            "'RectTransform' does not contain a definition for '%s' and no "
            "accessible extension method '%s' accepting a first argument of "
            "type 'RectTransform' could be found (are you missing a using "
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


# Symbols true for the packed desktop player (GLES host). Undefined UNITY_*
# symbols evaluate false — matches a non-Editor standalone Linux build.
_PACK_PP_DEFINES = frozenset((
    "UNITY_STANDALONE",
    "UNITY_STANDALONE_LINUX",
))


def _eval_unity_pp_expr(expr, defined=_PACK_PP_DEFINES):
    """Evaluate a Unity ``#if`` / ``#elif`` expression for the packed player."""
    return cs2cpp.eval_pp_expr(expr, defined)


def _blank_unity_editor_regions(text):
    """Blank inactive Unity ``#if`` regions for the packed desktop player.

    ``UNITY_EDITOR`` / ``UNITY_ANDROID`` / ``UNITY_IOS`` (and other undefined
    pack symbols) are false; ``UNITY_STANDALONE`` / ``UNITY_STANDALONE_LINUX``
    are true. ``#else`` / ``#elif`` follow C# preprocessor rules so mobile-only
    ``new NestedClass`` and editor OnValidate never reach method lowering.
    cs2cpp.blank_inactive_pp_regions with the pack's defines.
    """
    return cs2cpp.blank_inactive_pp_regions(text, _PACK_PP_DEFINES)


# BCL collection types the pack does not emit (would need heap `new` / generics).
# List → vector; Dictionary / SortedList → map — see cs2cpp.lower_packed_collections.
_REFUSED_BCL_TYPES = frozenset((
    # Stack / Queue / HashSet / LinkedList are Lists by then
    # (unity_pack_collections).
    "ConcurrentBag",
))


def _check_refused_api(path, text, scan):
    """Packed-subset refusals → Unity/csc diagnostics at the use site.

    ``using UnityEngine.UI`` is allowed (authored Image/Button fields). Invent
    (AddComponent<Canvas>, typeof(Canvas) spawn) is not. ForceUpdateCanvases
    is a no-op stub (layout is bake-time / host).
    """
    # Scripted Canvas invent — not authored !u!223.
    m = re.search(
            r"AddComponent\s*<\s*(?:UnityEngine\.)?(Canvas)\s*>", scan)
    if m:
        _raise_cs(path, text, m.start(1), "CS0246", _CS0246 % "Canvas")
    m = re.search(r"typeof\s*\(\s*(Canvas)\s*\)", scan)
    if m:
        _raise_cs(path, text, m.start(1), "CS0246", _CS0246 % "Canvas")
    m = re.search(r"(?<![\w.])InputAction\b", scan)
    if m:
        _raise_cs(path, text, m.start(), "CS0246", _CS0246 % "InputAction")
    # System.Collections.Generic — List lowers to std::vector; others refused.
    for tname in sorted(_REFUSED_BCL_TYPES):
        m = re.search(
            r"(?<![\w.])(%s)\s*<" % tname, scan)
        if m:
            _raise_cs(path, text, m.start(1), "CS0246", _CS0246 % tname)
        m = re.search(
            r"new\s+(%s)\s*(?:<|\()" % tname, scan)
        if m:
            _raise_cs(path, text, m.start(1), "CS0246", _CS0246 % tname)
    # `ParticleSystem.X(..)` through the *type*. Since particle systems are
    # read and simulated (unity_pack_particles.py) this check had been
    # switched off (`m = None and ..`), and a static call nothing lowers --
    # `ParticleSystem.Emit(0f, 0f)` -- was no longer refused: the method
    # holding it was emptied, with only a CS8000 warning. csc's answer
    # depends on the member: an instance member called through the type is
    # CS0120, a member the type does not have is CS0117. A field, local or
    # parameter *named* `ParticleSystem` makes the name the variable (C#'s
    # "Color Color" rule), and then it is an ordinary instance call.
    if not _PS_NAMED_VARIABLE.search(scan):
        m = re.search(r"(?<![\w.])(?:UnityEngine\.)?ParticleSystem\s*\.\s*"
                      r"([A-Za-z_]\w*)\s*\(", scan)
        if m:
            member = m.group(1)
            if member in _PARTICLE_SYSTEM_INSTANCE_MEMBERS:
                _raise_cs(
                    path, text, m.start(1), "CS0120",
                    "An object reference is required for the non-static "
                    "field, method, or property 'ParticleSystem.%s'" % member)
            _raise_cs(
                path, text, m.start(1), "CS0117",
                "'ParticleSystem' does not contain a definition for '%s'"
                % member)
    m = re.search(r"AnimationCurve\.(Evaluate)\b", scan)
    if m:
        _raise_cs(
            path, text, m.start(1), "CS0117",
            "'AnimationCurve' does not contain a definition for 'Evaluate'")
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
    # General C# lexing (cs2cpp), then the Unity API checks below.
    err = cs2cpp.real_literal_error(path, text, scan,
                                    display_path=_assets_rel_path)
    if err:
        raise PackError(err)
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
    "PolygonCollider2D",
    "BoxCollider",
    "SphereCollider",
    "Animation",
    "Animator",
    "AudioSource",
    "LineRenderer",     # spare rows (tools/unity/unity_pack_lines.py)
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


# Authored fields packed as a GameObject index. A component reference is
# only ever used through the engine's GO-keyed helpers (`Transform_get_parent`,
# `RectTransform_get_rect_height`, …), so the GO is the handle.
_GO_HANDLE_FIELD_TYPES = _UI_COMPONENT_FIELD_TYPES | frozenset(
    ("Transform", "GameObject", "Animator", "SpriteRenderer"))

# Every GameObject has a Transform (RectTransform is the uGUI subclass).
# GetComponent<Transform|RectTransform>() ≡ GO index (same as .transform).
_TRANSFORM_GETCOMPONENT_TYPES = frozenset(("Transform", "RectTransform"))

# ParticleSystem: read, simulated and drawn (tools/unity/unity_pack_particles.py).
_PARTICLE_COMPONENTS = frozenset(("ParticleSystem",))
import tools.unity.unity_pack_particles as _parts
import tools.unity.unity_pack_curves as _curves
import tools.unity.unity_pack_lines as _lines
import tools.unity.unity_pack_tilemap as _tm
import tools.unity.unity_pack_mesh as _mesh



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
    "Input.GetButtonDown": True,
    "Input.GetButtonUp": True,
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
    "Canvas": (
        "Scripted Canvas invent (AddComponent / typeof) is refused — author "
        "a !u!223 Canvas + Image in the scene. ForceUpdateCanvases is a no-op."
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
    r"Input\.(?:GetAxis|GetButton(?:Down|Up)?|GetKey)|"
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
_WANT_INPUT = frozenset({"Input.GetAxis", "Input.GetButton", "Input.GetKey",
                         "Input.GetButtonDown", "Input.GetButtonUp"})
_BUTTON_APIS = ("Input.GetButton", "Input.GetButtonDown", "Input.GetButtonUp")


def _button_ident(name):
    """`Fire1` -> `Fire1`, `My Button` -> `My_Button`: the host global's name."""
    return re.sub(r"\W", "_", name)
_KEYBOARD_KEY = re.compile(
    r"Keyboard\.current\.(\w+)Key\.(isPressed|wasPressedThisFrame|"
    r"wasReleasedThisFrame)"
)


# ---------------------------------------------------------------------------
# Project walk
# ---------------------------------------------------------------------------

#: a serialized number as Unity writes it (`1e-05`, `Infinity`, `NaN` too)
_YAML_NUM = r"-?(?:\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|Infinity)|NaN"


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


def unity_physics_settings(root):
    """Authored Physics2D.gravity, Physics.gravity, Time.fixedDeltaTime.

    And the Time.timeScale a player starts with (TimeManager m_TimeScale),
    and Physics2D.queriesHitTriggers (default 1).
    Unity defaults (0,-9.81), (0,-9.81,0), 0.02, 1 when a setting is absent.
    Fixed Timestep is a float in older assets, a rational (m_Count ticks at
    m_Numerator / m_Denominator Hz) in newer ones.
    """
    ps = os.path.join(root, "ProjectSettings")
    num = r"\s*([-0-9.eE+]+)"

    def asset(name):
        path = os.path.join(ps, name)
        return _read(path) if os.path.isfile(path) else ""

    out = {"gravity2d": (0.0, -9.81), "gravity3d": (0.0, -9.81, 0.0),
           "fixed_dt": 0.02}
    m = re.search(r"m_Gravity:\s*\{x:%s,\s*y:%s\}" % (num, num),
                  asset("Physics2DSettings.asset"))
    if m:
        out["gravity2d"] = (float(m.group(1)), float(m.group(2)))
    m = re.search(r"(?m)^\s*m_QueriesHitTriggers:\s*(\d)",
                  asset("Physics2DSettings.asset"))
    out["queries_hit_triggers"] = int(m.group(1)) if m else 1
    m = re.search(r"m_Gravity:\s*\{x:%s,\s*y:%s,\s*z:%s\}" % (num, num, num),
                  asset("DynamicsManager.asset"))
    if m:
        out["gravity3d"] = tuple(float(m.group(k)) for k in (1, 2, 3))
    tm = asset("TimeManager.asset")
    m = re.search(r"(?m)^\s*Fixed Timestep:%s\s*$" % num, tm)
    r = re.search(r"Fixed Timestep:\s*\n\s*m_Count:%s\s*\n\s*m_Rate:\s*\n"
                  r"\s*m_Denominator:%s\s*\n\s*m_Numerator:%s" % (num, num, num),
                  tm)
    if m:
        out["fixed_dt"] = float(m.group(1))
    elif r and float(r.group(3)) > 0:
        out["fixed_dt"] = (float(r.group(1)) * float(r.group(2))
                           / float(r.group(3)))
    m = re.search(r"(?m)^\s*m_TimeScale:%s\s*$" % num, tm)
    if m:
        out["time_scale"] = float(m.group(1))
    return out


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



def _seed_camera_script_view(plan, objects):
    """Bake CameraScript.HandleViewSize into plan camera globals.

    ``camera.aspect`` / ``camera.rect`` / updated ``orthographicSize`` are not
    lowered from C# (Rect / Camera setters). When an authored ``viewSize`` is
    present, seed the same values HandleViewSize would assign so Screen Space
    Camera UI and the GLES host letterbox match Unity.
    """
    cam = plan.get("camera")
    if not cam:
        return
    sw = max(1, int(plan.get("screen_width") or 1024))
    sh = max(1, int(plan.get("screen_height") or 768))
    view = _authored_camera_view_size(objects)
    if view is None:
        plan["camera_aspect"] = float(sw) / float(sh)
        plan["camera_rect"] = (0.0, 0.0, 1.0, 1.0)
        return
    vw, vh = view
    aspect = vw / vh if vh > 1e-6 else float(sw) / float(sh)
    # HandleViewSize: orthographicSize = max(view.x/2/aspect, view.y/2)
    ortho = max(vw * 0.5 / aspect, vh * 0.5) if aspect > 1e-6 else vh * 0.5
    cam["orthographic_size"] = ortho
    plan["camera_aspect"] = aspect
    plan["camera_rect"] = _camera_script_rect(sw, sh, vw, vh)



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


# ---------------------------------------------------------------------------
# AnimationClip / AnimatorController (authored assets)
# ---------------------------------------------------------------------------


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



# ---------------------------------------------------------------------------
# Scene importers
# ---------------------------------------------------------------------------

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
                rec.setdefault("object_ref_arrays", {})[fm.group(1)] = \
                    re.findall(r"fileID:\s*(-?\d+)", fm.group(2))
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
    # them so DeactivateBasedOnInputDevice / nested scripts still pack.
    mbs_by_go = {}
    for rec in by_id.values():
        if rec.get("kind") != "MonoBehaviour":
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
            "mb_ids": mb_ids,
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
                "struct_values": dict(k.get("struct_values") or {}),
                "mb_ids": [str(k.get("file_id"))],
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



def _onclick_mb_types(objects):
    """Packed MonoBehaviour class names targeted by Button/Slider/Toggle events."""
    out = set()
    for o in objects or []:
        for c in (o.get("ui_button") or {}).get("onclick") or []:
            if c.get("method") == "SetActive":
                continue
            cls = c.get("target_class")
            if cls:
                out.add(cls)
        for key in ("ui_slider", "ui_scrollbar", "ui_toggle"):
            for c in (o.get(key) or {}).get("on_value_changed") or []:
                cls = c.get("target_class")
                if cls:
                    out.add(cls)
        et = o.get("ui_eventtrigger")
        if et:
            for d in et.get("delegates") or []:
                for c in d.get("calls") or []:
                    cls = c.get("target_class")
                    if cls:
                        out.add(cls)
    return out


def _alias_onclick_mb_file_ids(objects):
    """Attach scene onClick/onValueChanged MB fileIDs onto packed mb_ids.

    Stripped PrefabInstance MBs (scene fileID) are not joined via m_Component;
    Button/Slider/Toggle targets still reference them. Alias onto the first
    instance of the annotated target class so ``_mb_index`` resolves the call.
    """
    by_class = {}
    owned = set()
    for o in objects or []:
        by_class.setdefault(o.get("class"), []).append(o)
        owned.update(str(m) for m in o.get("mb_ids") or [])

    def _alias(calls, scene):
        for c in calls or []:
            if c.get("method") == "SetActive":
                continue
            cls = c.get("target_class")
            tid = str(c.get("target_go") or "")
            if not cls or not tid or tid == "0" or tid in owned:
                continue
            insts = by_class.get(cls) or []
            same = [i for i in insts if int(i.get("scene") or 0) == scene]
            unplaced = [i for i in insts if "scene" not in i]
            for inst in same or unplaced:
                inst.setdefault("mb_ids", []).append(tid)
                owned.add(tid)
                break

    for o in objects or []:
        sc = int(o.get("scene") or 0)
        _alias((o.get("ui_button") or {}).get("onclick"), sc)
        _alias((o.get("ui_slider") or {}).get("on_value_changed"), sc)
        _alias((o.get("ui_scrollbar") or {}).get("on_value_changed"), sc)
        _alias((o.get("ui_toggle") or {}).get("on_value_changed"), sc)
        et = o.get("ui_eventtrigger")
        if et:
            for d in et.get("delegates") or []:
                _alias(d.get("calls"), sc)



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


def parse_godot_tscn(text, path=None):
    """Objects of one Godot .tscn: `godot_pack.parse_tscn`."""
    return _godot.parse_tscn(text, path)


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
    # Standalone this.GetComponent<T>() / GetComponent<T>() — not recv.GetComponent.
    for m in re.finditer(
            r"(?:(?<![\w.])this\s*\.\s*)?(?<![\w.])GetComponent(InParent)?\s*<",
            scan):
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
            "in_parent": bool(m.group(1)),
        })
    # recv.GetComponent<T>() — GO / component handle is already an index.
    for m in re.finditer(r"(?<![\w.])(\w+)\s*\.\s*GetComponent(InParent)?\s*<",
                         scan):
        recv = m.group(1)
        if recv == "this":
            continue
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
            "find_args": None,
            "component": comp_ty,
            "type_idx": type_idx,
            "field": field,
            "axis": axis,
            "recv": recv,
            "on_this": False,
            "in_parent": bool(m.group(2)),
        })
    out.sort(key=lambda c: c["start"], reverse=True)
    return out


def _go_identity_key(o, fallback):
    """Stable identity for an authored GameObject (not the display name).

    UI trees reuse names (``Text``, ``Sliding Area``, …). Parent / activeSelf
    tables must key by fileID, or inactive parents like Settings Menu cannot
    hide their children once a second GO shares the name (e.g. Player UI).
    """
    gid = o.get("go_id")
    if gid is not None and str(gid) not in ("", "0"):
        return "go:%s" % gid
    xid = o.get("xf_id")
    if xid is not None and str(xid) not in ("", "0"):
        return "xf:%s" % xid
    return fallback


def _build_go_tables(plan):
    """One GO-table slot per authored GameObject (unique fileID / Transform).

    ``go_names`` may contain duplicates — ``GameObject.Find`` returns the first
    match (Unity). Each instance / hierarchy entry gets ``go_index``.
    ``go_components`` stays name → {class: instance idx} for the first GO of
    each name (Find / GetComponent helpers).
    """
    names = []
    key_to_i = {}

    def _add(o, fallback):
        k = _go_identity_key(o, fallback)
        if k in key_to_i:
            gi = key_to_i[k]
        else:
            gi = len(names)
            key_to_i[k] = gi
            names.append(o.get("name") or "obj")
        o["go_index"] = gi
        return gi

    for i, h in enumerate(plan.get("scene_hierarchy") or []):
        _add(h, "h:%d" % i)
    comps = {}  # name -> {class: idx} (first GO with that name)
    for cname, cl in sorted(plan["classes"].items()):
        for i, o in enumerate(cl.get("instances") or []):
            gi = _add(o, "c:%s:%d" % (cname, i))
            n = names[gi]
            slot = comps.setdefault(n, {})
            if cname not in slot:
                slot[cname] = i
    return names, comps


def _build_go_active(plan, go_names):
    """Authored ``m_IsActive`` per go_names index (1 = activeSelf)."""
    act = [1] * len(go_names or [])
    for h in plan.get("scene_hierarchy") or []:
        gi = h.get("go_index")
        if gi is None or gi < 0 or gi >= len(act):
            continue
        act[gi] = 1 if int(h.get("active", 1)) else 0
    for cl in (plan.get("classes") or {}).values():
        for o in cl.get("instances") or []:
            gi = o.get("go_index")
            if gi is None or gi < 0 or gi >= len(act):
                continue
            # Only override when the instance recorded m_IsActive; missing
            # must not clobber hierarchy (inactive parents like Settings Menu).
            if "active" not in o:
                continue
            act[gi] = 1 if int(o.get("active", 1)) else 0
    # an unplaced prefab's row is dormant; its authored m_IsActive is what an
    # Instantiate of it gets (`_engine_go_asset_active`)
    asset = [-1] * len(act)
    for cl in (plan.get("classes") or {}).values():
        for o in cl.get("instances") or []:
            gi = o.get("go_index")
            if o.get("prefab_asset") and gi is not None and 0 <= gi < len(act):
                asset[gi], act[gi] = act[gi], 0
    plan["go_asset_active"] = asset if any(a >= 0 for a in asset) else None
    return act



def _build_go_tags(plan, go_names):
    """Authored ``m_TagString`` per go_names index ("Untagged" when none)."""
    tags = ["Untagged"] * len(go_names or [])
    for h in plan.get("scene_hierarchy") or []:
        gi = h.get("go_index")
        if gi is None or gi < 0 or gi >= len(tags):
            continue
        tags[gi] = h.get("tag") or "Untagged"
    return tags


def _draw_layer(plan, gi, sp=None):
    """`EngineDraw.layer`: the layer of GameObject *gi* (go_names index)
    that the cameras' culling masks test -- or, for a uGUI draw (*sp* its
    sprite), -1 Screen Space Overlay (drawn after every camera) / -3 - k
    Screen Space Camera (its canvas's camera, rendering row k) / -2 (the
    main camera's, when that camera is not packed).

    ponytail: the authored layer; `gameObject.layer =` does not re-cull."""
    if sp and sp.get("source") in ("ui", "ui_tmp"):
        if int(sp.get("ui_mode") or 0) == 0:
            return -1
        rows = [c.get("cam_file_id") for c in plan.get("cameras") or []
                if c.get("renders", True)]
        cid = sp.get("ui_cam_file_id")
        return -3 - rows.index(cid) if cid and cid in rows else -2
    gl = plan.get("go_layers") or []
    return int(gl[gi]) if gi is not None and 0 <= int(gi) < len(gl) else 0


def _build_go_layers(plan, go_names):
    """Authored ``m_Layer`` per go_names index (0, Default, when none)."""
    layers = [0] * len(go_names or [])
    for h in plan.get("scene_hierarchy") or []:
        gi = h.get("go_index")
        if gi is None or gi < 0 or gi >= len(layers):
            continue
        layers[gi] = int(h.get("layer") or 0)
    return layers


def _collider2d_layers(plan, col2d_list):
    """Each Collider2D's layer: its GameObject's."""
    gl = plan.get("go_layers") or []
    out = []
    for c in col2d_list:
        insts = (plan["classes"].get(c.get("owner_class")) or {}).get(
            "instances") or []
        k = c.get("owner_inst")
        gi = insts[k].get("go_index") if isinstance(k, int) and \
            0 <= k < len(insts) else None
        out.append(gl[gi] if isinstance(gi, int) and 0 <= gi < len(gl) else 0)
    return out


def _build_go_scene(plan, go_names):
    """Build index of the scene each go_names entry was authored in."""
    sc = [0] * len(go_names or [])
    recs = list(plan.get("scene_hierarchy") or [])
    for cl in (plan.get("classes") or {}).values():
        recs.extend(cl.get("instances") or [])
    for o in recs:
        gi = o.get("go_index")
        if gi is not None and 0 <= gi < len(sc) and o.get("scene"):
            sc[gi] = int(o["scene"])
    return sc


def _extend_go_tables_for_find(plan, names, comps):
    """No-op: ``_build_go_tables`` already includes hierarchy-only GOs."""
    return names, comps


def _build_go_parents(plan):
    """go index → parent go index via authored m_Father (for activeInHierarchy)."""
    names = plan.get("go_names") or []
    if not names:
        return []
    xf_to_go = {}
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            gi = o.get("go_index")
            xid = o.get("xf_id")
            if gi is None or xid is None or str(xid) == "0":
                continue
            xf_to_go[str(xid)] = int(gi)
    for h in plan.get("scene_hierarchy") or []:
        gi = h.get("go_index")
        xid = h.get("xf_id")
        if gi is None or xid is None or str(xid) == "0":
            continue
        xf_to_go.setdefault(str(xid), int(gi))
    parents = [-1] * len(names)
    for cl in plan["classes"].values():
        for o in cl.get("instances") or []:
            gi = o.get("go_index")
            if gi is None or gi < 0 or gi >= len(parents):
                continue
            fid = o.get("father_id")
            if not fid or str(fid) == "0":
                continue
            parents[int(gi)] = int(xf_to_go.get(str(fid), -1))
    for h in plan.get("scene_hierarchy") or []:
        gi = h.get("go_index")
        if gi is None or gi < 0 or gi >= len(parents):
            continue
        if parents[int(gi)] >= 0:
            continue
        fid = h.get("father_id")
        if not fid or str(fid) == "0":
            continue
        parents[int(gi)] = int(xf_to_go.get(str(fid), -1))
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


def _instantiate_budget(analyses, plan):
    """Extra MB + GO slots for ``Instantiate(this[, parent])`` of packed types.

    Budget is one spare per authored instance of each class that calls
    ``Instantiate(this…)`` (Update-loop reuse). Only the enclosing class is
    cloned — prefab / multi-arg overloads stay unlowered.
    """
    budget = {}
    class_n = {n: int(cl.get("n") or 0) for n, cl in plan["classes"].items()}
    for a in analyses:
        for c in a.get("classes") or []:
            cname = c["name"]
            if cname not in (plan.get("classes") or {}):
                continue
            n = class_n.get(cname, 1) or 1
            bodies = "\n".join(m.get("body") or "" for m in c.get("methods") or [])
            for _m in re.finditer(
                    r"(?<![\w.])(?:(?:UnityEngine\.)?Object\.)?Instantiate\s*"
                    r"\(\s*this\b",
                    bodies):
                budget[cname] = budget.get(cname, 0) + n
            for t in _prefab_instantiate_fields(c, plan).values():
                budget[t] = budget.get(t, 0) + n
            # `Instantiate(field[, parent])`: one clone per instance, as for `this`
            ftys = {f["name"]: f["ty"].split(".")[-1] for f in c.get("fields") or ()
                    if not f.get("static")}
            for m in re.finditer(r"(?<![\w.])(?:(?:UnityEngine\.)?Object\.)?Instantiate\s*\(\s*"
                                 r"(?:this\s*\.\s*)?(\w+)\s*(?:,[^,;()]*)?\)", cs2cpp._blank(bodies)):
                t = ftys.get(m.group(1))
                if t in class_n and t not in _ADDABLE_BUILTINS:
                    budget[t] = budget.get(t, 0) + n
            for t in _child_clone_classes(c, plan):
                budget[t] = budget.get(t, 0) + n
    return budget


def _child_clone_classes(c, plan):
    """Classes `Instantiate(field.GetChild(k)[.gameObject], ..)` in class *c*
    can clone: whatever sits on a child of the field's authored Transform.
    Only a child GameObject that is one packed class and has no children of
    its own (`GameObject_Instantiate` clones that one row, not a subtree)."""
    classes = plan.get("classes") or {}
    ftys = {f["name"]: f["ty"] for f in c.get("fields") or ()}
    bodies = cs2cpp._blank(
        "\n".join(m.get("body") or "" for m in c.get("methods") or []))
    names = {m.group(1) for m in re.finditer(
        r"(?<![\w.])(?:(?:UnityEngine\.)?Object\.)?Instantiate\s*\(\s*"
        r"(?:this\s*\.\s*)?(\w+)\s*\.\s*GetChild\s*\(", bodies)
        if ftys.get(m.group(1)) in ("Transform", "RectTransform")}
    fids = {str((o.get("object_refs") or {}).get(f))
            for f in names for o in (classes.get(c["name"]) or {}).get(
                "instances") or ()} - {"None", "0"}
    if not fids:
        return set()
    rows = [(cn, o) for cn, cl in classes.items() for o in cl.get("instances") or ()]
    per_go, fathers = {}, set()
    for cn, o in rows:
        per_go.setdefault(str(o.get("xf_id")), set()).add(cn)
        fathers.add(str(o.get("father_id")))
    return {cn for cn, o in rows
            if str(o.get("father_id")) in fids and o.get("xf_id")
            and len(per_go[str(o["xf_id"])]) == 1
            and str(o["xf_id"]) not in fathers}


_PREFAB_SPAWN_POOL = 64
_GO_CLONE_POOL = 16


def _prefab_instantiate_fields(c, plan):
    """{field: class} of `Instantiate(field, pos, Quaternion.identity)` in
    class *c*, *field* a packed-class reference (an unplaced prefab's
    dormant row)."""
    classes = plan.get("classes") or {}
    ftys = {f["name"]: f["ty"].split(".")[-1] for f in c.get("fields") or ()
            if not f.get("static")}
    bodies = cs2cpp._blank(
        "\n".join(m.get("body") or "" for m in c.get("methods") or []))
    out = {}
    for m in re.finditer(
            r"(?<![\w.])(?:(?:UnityEngine\.)?Object\.)?Instantiate\s*"
            r"\(\s*(?:this\s*\.\s*)?(\w+)\s*,[^,;]*,\s*Quaternion\s*\.\s*"
            r"identity\s*\)", bodies):
        t = ftys.get(m.group(1))
        if t in classes and t not in _ADDABLE_BUILTINS:
            out[m.group(1)] = t
    return out


def _new_budget(analyses, plan):
    """Extra instance slots for ``new T(..)`` of a packed non-scene class.

    A nested helper class (`_Scrollbar.DragUpdater`) is packed like any
    other but the scene places none of it: its array exists with no slot
    to allocate. Budget is one spare per authored instance of each class
    whose body constructs it -- the same promise `Instantiate` makes,
    and the same clip when the pool is full.
    """
    budget = {}
    classes = plan.get("classes") or {}
    class_n = {n: int(cl.get("n") or 0) for n, cl in classes.items()}
    for a in analyses:
        for c in a.get("classes") or []:
            n = max(1, class_n.get(c["name"], 1) or 1)
            bodies = cs2cpp._blank(
                "\n".join(m.get("body") or "" for m in c.get("methods") or []))
            for m in re.finditer(r"(?<![\w.])new\s+([\w.]+)\s*\(", bodies):
                t = m.group(1).split(".")[-1]
                if t not in classes or t in _ADDABLE_BUILTINS:
                    continue
                budget[t] = budget.get(t, 0) + n
            # `static T f = new T();`: one object, made at the first tick
            for t in _static_new_fields(c.get("fields"), classes).values():
                budget[t] = budget.get(t, 0) + 1
    return budget


#: spare GameObjects each `new GameObject(..)` call site of a class may make, per instance of that class (the count is data-driven
#: when a loop makes them, so the cap is a promise like [MaxInstances]: the call returns -1 once the pool is full)
_NEW_GO_POOL = 64


def _newgo_budget(analyses, plan):
    """Spare GameObject slots for ``new GameObject(..)``: `_NEW_GO_POOL` for each call site of each instance of the calling class."""
    total = 0
    class_n = {n: int(cl.get("n") or 0) for n, cl in (plan.get("classes") or {}).items()}
    for a in analyses:
        for c in a.get("classes") or []:
            n = max(1, class_n.get(c["name"], 1) or 1)
            bodies = cs2cpp._blank("\n".join(m.get("body") or "" for m in c.get("methods") or []))
            total += len(re.findall(r"(?<![\w.])new\s+(?:UnityEngine\s*\.\s*)?GameObject\s*\(", bodies)) * n * _NEW_GO_POOL
    return total


def _static_new_fields(fields, classes):
    """{name: class} of the `static T f = new T();` fields of a packed T."""
    return {f["name"]: f["ty"].split(".")[-1] for f in fields or ()
            if f.get("static") and f.get("new_init")
            and f["ty"].split(".")[-1] in classes
            and f["ty"].split(".")[-1] not in _ADDABLE_BUILTINS}


def _class_index_width(cname, n, spawn, annotated=None):
    """(C type, bits, bounded) of an index into `cname`'s instance array.

    `[MaxInstances(N)]` on the class is the author's promise that at most N
    are ever live -- the array holds N and `Instantiate` returns null when
    it is full: dropping the N+1st is the behaviour asked for, a bullet
    that is never fired rather than a heap that grows. With it the index
    is as narrow as N allows, whatever else in the project spawns. Without
    it, the scene's count bounds the index only when nothing spawns.

    An index's top value is its null (`_idx_null`), so a uint8_t indexes
    255 instances and a uint16_t 65535.
    """
    if annotated is not None:
        cap = int(annotated["max_instances"])
        if n > cap:
            _raise_cs(annotated.get("path") or "<cs>",
                      annotated.get("file_text") or "",
                      int(annotated.get("max_instances_at") or 0), "CS8000",
                      "[MaxInstances(%d)] on `%s`, and the scene already places "
                      "%d of them. Raise the cap, or place fewer."
                      % (cap, cname, n))
        if cap <= 255:
            return "uint8_t", 8, True
        if cap <= 65535:
            return "uint16_t", 16, True
        return "uint32_t", 32, True
    bounded = (not spawn) and n > 0
    if bounded and n <= 255:
        return "uint8_t", 8, True
    if bounded and n <= 65535:
        return "uint16_t", 16, True
    return "uint32_t", 32, False


def _mb_pool_extra(plan, cname):
    """Spare instance slots: AddComponent budget + Instantiate budget -- or,
    under `[MaxInstances(N)]`, exactly what fills the class to N."""
    cl = (plan.get("classes") or {}).get(cname) or {}
    if cl.get("max_instances") is not None:
        return max(0, int(cl["max_instances"]) - int(cl.get("n") or 0))
    add = int((plan.get("addcomponent_budget") or {}).get(cname) or 0)
    inst = int((plan.get("instantiate_budget") or {}).get(cname) or 0)
    new = int((plan.get("new_budget") or {}).get(cname) or 0)
    return add + inst + new


def _disallow_multiple_types(analyses):
    """Type names that refuse a second AddComponent (Unity attribute / builtins)."""
    out = set(_DISALLOW_MULTIPLE_BUILTINS)
    for a in analyses:
        for c in a.get("classes") or []:
            if c.get("disallow_multiple"):
                out.add(c["name"])
    return out



def _validate_addcomponent_types(types, plan, analyses=None):
    # a joint kind (not Joint2D / AnchoredJoint2D: abstract, as in Unity)
    known = set(plan.get("classes") or {}) | _ADDABLE_BUILTINS | set(_JOINT2D_KINDS)
    analyses = analyses or []
    for t in sorted(types):
        if t in _REFUSED_ADDCOMPONENT or t not in known:
            _raise_unknown_component_type(
                t, analyses, ops=("AddComponent",))


#: Accepted by GetComponent, not lowered by it: the method stubs (CS8000).
_RENDERER_LOOKUP_TYPES = frozenset({"MeshFilter", "MeshRenderer", "Renderer"})


def _analyze_static_refs(analyses, typename_map):
    """Scripts no scene places but a packed one calls statically
    (`SaveAndLoadManager.GetInt(..)`): analyzed in full, so their static
    methods are emitted (a zero-instance class) and the calls lower.
    Transitively; a script the pack refuses is left out (the calls stub)."""
    have = {c["name"] for a in analyses for c in a.get("classes") or []}
    got, queue = [], list(analyses)
    while queue:
        a = queue.pop()
        refs = set()
        for c in a.get("classes") or []:
            refs |= set(re.findall(r"(?<![\w.])([A-Z]\w*)\s*\.\s*[A-Z]\w*",
                                   cs2cpp._blank(c.get("file_text") or "")))
        for t in sorted(refs - have):
            if t not in typename_map:
                continue
            have.add(t)
            try:
                sa = analyze_script(typename_map[t])
            except PackError:
                continue
            # a `static class` is `_static_helper_methods`' (it skips packed ones)
            if re.search(r"\bstatic\s+(?:partial\s+)?class\s+%s\b" % re.escape(t),
                         cs2cpp._blank(_read(typename_map[t]))):
                continue
            sa["static_ref"] = True
            got.append(sa)
            queue.append(sa)
    return got


def _derives_engine_type(a, typename_map, seen=None):
    """Whether a class of analysis *a* derives, through project bases, from
    a type outside the project (MonoBehaviour, ScriptableObject, ..): a
    component, not a plain [Serializable] value (`FloatRange : Range<float>`).
    ponytail: a non-project base named `I<Upper>..` is taken for an
    interface"""
    seen = set() if seen is None else seen
    for c in a.get("classes") or []:
        for b in c.get("bases") or ():
            if b in seen or b in ("object", "Object", "System"):
                continue
            seen.add(b)
            if b not in typename_map:
                if not re.match(r"I[A-Z]", b):
                    return True
            elif _derives_engine_type(_script_bases(typename_map[b]),
                                      typename_map, seen):
                return True
    return False


_SCRIPT_BASES = {}


def _script_bases(path):
    """``{"classes": [{"bases"}]}`` of the script at *path* (shallow, cached
    by path and mtime)."""
    key = (path, os.path.getmtime(path))
    if key not in _SCRIPT_BASES:
        _SCRIPT_BASES[key] = {"classes": [
            {"bases": list(c.get("bases") or ())}
            for c in analyze_script(path, shallow=True).get("classes") or []]}
    return _SCRIPT_BASES[key]


def _inherit_base_members(analyses, typename_map):
    """Copy what a class inherits from its authored bases into it.

    A base is its own packed array, so `GameCamera : CameraScript` had no
    `DoUpdate` and no `viewRect`: the instance the scene places ran none of
    what C# runs for it. Each fully analyzed class gets its bases' instance
    fields and the instance methods it does not override (a generic base's
    type parameters bound: `SingletonUpdateWhileEnabled<Player>`), and
    `base.M(` calls a copy of the nearest ancestor's M, `base__B__M`.
    Statics stay with the class that declares them. Returns the bases no
    analysis had yet, shallow (no methods), so `mb_bases` reaches the
    interfaces at the top of a chain (`IUpdatable`).
    ponytail: an override is matched by name and arity, not parameter types;
    a bare static name in a copied body resolves in the derived class, so a
    `new static` there stands in for the base's (same object for singletons)
    """
    full = {}

    def cls_of(name):
        path = typename_map.get(name)
        if not path:
            return None
        if path not in full:
            try:
                full[path] = analyze_script(path)
            except PackError:
                full[path] = None
        a = full[path]
        return next((c for c in (a or {}).get("classes") or ()
                     if c["name"] == name), None)

    def key(m):
        return (m["name"], len(cs2cpp.parse_params(m.get("args") or "")))

    def bind(text, targs):
        for g, t in targs.items():
            text = cs2cpp.code_sub(r"(?<![\w.])%s(?![\w])" % re.escape(g), t, text)
        return text

    for a in analyses:
        if a.get("shallow"):
            continue
        for c in a.get("classes") or []:
            # the chain, nearest first: (class, its type arguments)
            chain, cur, seen = [], c, {c["name"]}
            while True:
                b = next((b for b in cur.get("bases") or ()
                          if b in typename_map and b not in seen), None)
                bc = cls_of(b) if b else None
                if bc is None or bc.get("kind") == "interface":
                    break
                hm = re.search(r"\b%s\s*<([^<>{};]*)>" % re.escape(b),
                               cs2cpp._blank(cur.get("file_text") or ""))
                pm = re.search(r"\bclass\s+%s\s*<([^<>{};]*)>" % re.escape(b),
                               cs2cpp._blank(bc.get("file_text") or ""))
                targs = dict(zip([s.strip() for s in pm.group(1).split(",")],
                                 [s.strip() for s in hm.group(1).split(",")])
                             ) if hm and pm else {}
                # an outer binding reaches the base's own (`T` of the derived)
                prev = chain[-1][1] if chain else {}
                targs = {g: prev.get(t, t) for g, t in targs.items()}
                chain.append((bc, targs))
                seen.add(b)
                cur = bc
            if not chain:
                continue
            # what the copied bodies use (string helpers, GetComponent<T>, ..)
            for bc, _t in chain:
                ba = full.get(typename_map.get(bc["name"])) or {}
                for k, v in ba.items():
                    if isinstance(v, set):
                        a[k] = set(a.get(k) or ()) | v
                    elif k in ("spawns", "uses_z", "writes_pos", "writes_rot") and v:
                        a[k] = True
                a["literals"] = list(a.get("literals") or []) + [
                    x for x in ba.get("literals") or [] if x not in (a.get("literals") or [])]
            levels = [(c, {})] + chain

            def base_ref(level, body):
                """`base.M(` in a body from levels[level] → the copy's name."""
                def sub(m):
                    for j in range(level + 1, len(levels)):
                        bc, targs = levels[j]
                        bm = next((x for x in bc["methods"] if x["name"] == m.group(1)
                                   and not x.get("static")), None)
                        if bm:
                            want.append((j, bm))
                            return "base__%s__%s(" % (bc["name"], bm["name"])
                    return m.group(0)
                return cs2cpp.code_sub(r"(?<![\w.])base\s*\.\s*(\w+)\s*\(", sub, body)

            def copy(level, bm, name=None):
                bc, targs = levels[level]
                m = dict(bm, name=name or bm["name"], path=bc.get("path"),
                         file_text=bc.get("file_text"), inherited_from=bc["name"])
                for k in ("body", "args", "ret", "src"):
                    if m.get(k):
                        m[k] = bind(m[k], targs)
                m["body"] = base_ref(level, m.get("body") or "")
                return m

            want = []
            for m in c["methods"]:
                m["body"] = base_ref(0, m.get("body") or "")
            have = {key(m) for m in c["methods"]}
            for j in range(1, len(levels)):
                for bm in levels[j][0]["methods"]:
                    if bm.get("static") or bm.get("ctor") or key(bm) in have:
                        continue
                    have.add(key(bm))
                    c["methods"].append(copy(j, bm))
            done = set()
            while want:
                j, bm = want.pop()
                name = "base__%s__%s" % (levels[j][0]["name"], bm["name"])
                if name not in done:
                    done.add(name)
                    c["methods"].append(copy(j, bm, name))
            names = {f["name"] for f in c.get("fields") or []}
            for bc, targs in chain:
                for f in bc.get("fields") or []:
                    if f.get("static") or f.get("const") or f["name"] in names:
                        continue
                    names.add(f["name"])
                    c.setdefault("fields", []).append(
                        dict(f, ty=bind(f.get("ty") or "", targs), inherited_from=bc["name"]))
    have = {c["name"] for a in analyses for c in a.get("classes") or []}
    out = []
    for path, a in sorted((p, a) for p, a in full.items() if a):
        if not {c["name"] for c in a["classes"]} - have:
            continue
        out.append(dict(a, shallow=True, apis=set(), getcomponent_types=set(),
                        getcomponentsinchildren_types=set(), addcomponent_types=set(),
                        classes=[dict(c, methods=[]) for c in a["classes"]
                                 if c["name"] not in have]))
    return out


def _validate_getcomponent_types(types, plan, analyses=None):
    """GetComponent / GetComponentsInChildren<T> for unknown T → CS0246.

    Authored uGUI types (Canvas, Image, RectTransform, …) are allowed —
    GetComponent looks them up; AddComponent<Canvas> invent stays refused.
    Analyzed MonoBehaviour scripts (even with no scene instances) and packed
    subclasses count as known so ``GetComponentsInChildren<Weapon>`` works
    when only ``Blaster : Weapon`` is authored.
    """
    analyses = analyses or []
    bases_map = plan.get("mb_bases") or _collect_mb_bases(analyses)
    # MeshFilter / MeshRenderer are packed (unity_pack_mesh): a script may
    # ask for one; making a mesh at runtime is what is not packed yet
    known = (set(plan.get("classes") or {}) | _RENDERER_LOOKUP_TYPES
             | _ADDABLE_BUILTINS
             | _PHYSICS_COMPONENTS | _COLLIDER2D_TYPES
             | (_JOINT2D_COMPONENTS | _PARTICLE_COMPONENTS)
             | _UI_GETCOMPONENT_TYPES
             | _TRANSFORM_GETCOMPONENT_TYPES
             | _analyzed_mb_typenames(analyses)
             # ponytail: an interface lookup is accepted, not lowered — the
             # method stubs (CS8000) until iref locals + dispatch exist
             | set(_collect_interfaces(analyses)))
    # Base type with at least one packed subclass is known for GCIC.
    for t in types:
        if t in known:
            continue
        if _gcic_collector_types(t, plan, bases_map):
            known.add(t)
    for t in sorted(types):
        if t not in known:
            _raise_unknown_component_type(
                t, analyses,
                ops=("GetComponent", "GetComponentsInChildren",
                     "AddComponent"))



def _rewrite_instantiate(text, plan, this_class):
    """Lower ``Instantiate(this[, parent])`` → ``Object_Instantiate_T(src, go)``.

    Runs after ``this`` → ``i``. Only 1-arg and 2-arg (parent Transform/GO)
    forms of packed types with instantiate budget. Position/rotation overloads
    stay for the stub detector.
    """
    inst_budget = plan.get("instantiate_budget") or {}
    if not inst_budget and plan.get("go_clone_classes") is None:
        return text
    classes = plan.get("classes") or {}
    cl = classes.get(this_class) or {
        "name": this_class, "fields": [], "members": []}
    out = []
    i = 0
    pat = re.compile(
        r"(?<![\w.])(?:(?:UnityEngine\.)?Object\.)?Instantiate\s*\(")
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
        before = text[i:m.start()]
        tm = re.search(
            r"((?:(?:UnityEngine\.)?\w+))\s+(\w+)\s*=\s*$", before)
        if len(args) == 1:
            src = args[0].strip()
            parent = "-1"
        elif len(args) == 2:
            src = args[0].strip()
            parent = _setparent_parent_expr(args[1], cl)
        elif len(args) == 3:
            call = _instantiate_at(args, plan, cl)
            out.append(before)
            out.append(call or text[m.start():after])
            i = after
            continue
        else:
            out.append(text[i:after])
            i = after
            continue
        if src.startswith("Transform_GetChild(") and plan.get("go_clone_classes") is not None:
            out.append(before + "GameObject_Instantiate(%s, %s)" % (src, parent))
            i = after
            continue
        ty = None
        if tm:
            ty = tm.group(1).split(".")[-1]
        if src in ("i", "this"):
            ty = this_class
        elif ty is None:
            out.append(text[i:after])
            i = after
            continue
        if ty not in classes or not int(inst_budget.get(ty) or 0):
            out.append(text[i:after])
            i = after
            continue
        if src == "this":
            src = "i"
        helper = "Object_Instantiate_%s" % _c_ident(ty)
        call = "%s(%s, %s)" % (helper, src, parent)
        if tm:
            out.append(before[:tm.start()])
            out.append("int %s = %s" % (tm.group(2), call))
        else:
            out.append(before)
            out.append(call)
        i = after
    return "".join(out)


def _instantiate_at(args, plan, cl):
    """`Instantiate(prefabField, posField, Quaternion.identity)` of a packed
    class whose prefab rows are unrotated → `Object_InstantiateAt_T`, or
    None (left for the stub detector)."""
    src = re.sub(r"^this\s*\.\s*", "", args[0].strip())
    pos = args[1].strip()
    t = _prefab_instantiate_fields(
        {"fields": cl.get("fields"), "methods": [{"body": (
            "Instantiate(%s, %s, %s)" % (src, pos, args[2].strip()))}]},
        plan).get(src)
    tcl = (plan.get("classes") or {}).get(t)
    pty = {f["name"]: f["ty"].split(".")[-1] for f in cl.get("fields") or ()
           }.get(re.sub(r"^this\s*\.\s*", "", pos))
    if (not tcl or pty not in ("Vector2", "Vector3")
            or not int((plan.get("instantiate_budget") or {}).get(t) or 0)
            or not _class_has_position(tcl)
            or any(abs(q - e) > 1e-6
                   for o in tcl.get("instances") or ()
                   for q, e in zip(o.get("rot") or (0, 0, 0, 1),
                                   (0, 0, 0, 1)))):
        return None
    return "Object_InstantiateAt_%s(%s, %s.x, %s.y, %s)" % (
        _c_ident(t), src, pos, pos, pos + ".z" if pty == "Vector3" else "0.f")


def _gcic_go_expr(recv, cl, plan, locals_ty):
    """Receiver of GetComponentsInChildren → root GO C expr."""
    this_idn = _c_ident(cl.get("name") or "")
    this_go = "_engine_go_of_%s(i)" % this_idn
    if not recv or recv in ("this", "gameObject", "transform", "i"):
        return this_go
    recv = recv.strip()
    # foo.transform → GO of foo
    m = re.match(r"(.+?)\s*\.\s*transform\s*$", recv)
    if m:
        return _gcic_go_expr(m.group(1).strip(), cl, plan, locals_ty)
    ty = locals_ty.get(recv)
    if not ty:
        for f in cl.get("fields") or []:
            if f.get("name") == recv:
                ty = f.get("ty") or ""
                break
        if not ty:
            for name, _t, _b, kind in cl.get("members") or []:
                if name == recv and str(kind).startswith("idx:"):
                    ty = kind.split(":", 1)[1]
                    break
    if ty in ("Transform", "GameObject", "RectTransform"):
        return recv
    if ty and ty in (plan.get("classes") or {}):
        return "_engine_go_of_%s(%s)" % (_c_ident(ty), recv)
    # Unknown — treat as GO index (Transform/GO local after rewrite).
    return recv


def _rewrite_new_packed_class(text, plan):
    """``new T(args)`` of a packed class → ``Object_New_T(args)``.

    Runs after `this` → `i`, so a constructor argument that is the caller
    is already the index the packed constructor takes. Value types
    (`new Vector2(..)`), collections and arrays are other rewrites'.
    """
    budget = plan.get("new_budget") or {}
    if not budget:
        return text
    for cname in sorted(budget, key=len, reverse=True):
        if cname not in (plan.get("classes") or {}):
            continue
        if not int(budget.get(cname) or 0):
            continue
        idn = _c_ident(cname)
        call = r"(?<![\w.])new\s+(?:[\w.]+\s*\.\s*)?%s\s*\(" % re.escape(cname)
        ctors = [m for _c, m in (plan.get("_methods_by") or {}).get(cname, [])
                 if m.get("ctor") and m.get("name") == cname]
        if len(ctors) < 2:
            text = cs2cpp.code_sub(call, "Object_New_%s(" % idn, text)
            continue
        # overloads by argument type; a call none or several fit is left
        out, pos = [], 0
        for m in list(re.finditer(call, cs2cpp._blank(text))):
            if m.start() < pos:
                continue
            got = _match_call_args(text, m.end() - 1)
            meth = got and _pick_overload(ctors, got[0], ())
            if not meth:
                continue
            args, end = got
            margs = meth.get("args") or ""
            out += [text[pos:m.start()], "%s(%s)" % (
                _object_new_symbol(idn, margs, True),
                _call_with_defaults(args, cs2cpp.parse_params(margs)).strip())]
            pos = end
        text = "".join(out) + text[pos:]
    return text


def _object_new_symbol(idn, args_str, overloaded):
    """`new T(..)`'s C function: one per constructor overload."""
    if not overloaded:
        return "Object_New_%s" % idn
    return "Object_New_%s_%s" % (idn, cs2cpp.method_arg_type_suffix(args_str))


def _rewrite_getcomponentsinchildren(text, plan, this_class):
    """Lower ``GetComponentsInChildren<T>()`` → ``std::vector`` + helper.

    Supports:
      T[] xs = GetComponentsInChildren<T>();
      T[] xs = GetComponentsInChildren<T>(true);
      xs = recv.GetComponentsInChildren<T>();
      recv.GetComponentsInChildren<T>()  (bare call)
    ``Renderer`` resolves to ``SpriteRenderer``. ``.Length`` → ``.size()``.
    """
    gcic = set(plan.get("getcomponentsinchildren_types") or [])
    if not re.search(r"GetComponentsInChildren\s*<|Object_FindObjectsOfType_",
                     text):
        return text
    classes = plan.get("classes") or {}
    cl = classes.get(this_class) or {
        "name": this_class, "fields": [], "members": []}
    # Typed locals: Cosmetic cosmetic = … / int c = Object_Instantiate_Cosmetic(
    locals_ty = {}
    for m in re.finditer(
            r"(?:(?:UnityEngine\.)?(\w+)|int)\s+(\w+)\s*=", text):
        ty, name = m.group(1), m.group(2)
        if ty and ty[0].isupper():
            locals_ty[name] = ty
    for m in re.finditer(
            r"int\s+(\w+)\s*=\s*Object_Instantiate_(\w+)\s*\(", text):
        locals_ty[m.group(1)] = m.group(2)
    for m in re.finditer(
            r"int\s+(\w+)\s*=\s*GameObject_GetComponent_(\w+)\s*\(", text):
        locals_ty[m.group(1)] = m.group(2)
    for f in cl.get("fields") or []:
        locals_ty.setdefault(f["name"], f.get("ty") or "")
    for f in cl.get("ref_array_fields") or []:
        locals_ty.setdefault(f["name"], f.get("ty") or "")
    for name, _t, _b, kind in cl.get("members") or []:
        if str(kind).startswith("idx:"):
            locals_ty.setdefault(name, kind.split(":", 1)[1])

    vector_names = set()
    out = []
    i = 0
    # Optional recv. before GetComponentsInChildren (no lookbehind after '.')
    pat = re.compile(
        r"(?:(?<![.\w])(?P<recv>\w+)\s*\.\s*|(?<![.\w]))"
        r"GetComponentsInChildren\s*<\s*"
        r"(?:UnityEngine\.)?(?P<ty>\w+)\s*>\s*\(")
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
        args = _split_call_args(args_str) if args_str.strip() else []
        raw_ty = m.group("ty")
        resolved = _gcic_resolve_type(raw_ty)
        known = (
            resolved in classes
            or resolved in _PHYSICS_COMPONENTS
            or resolved in (_JOINT2D_COMPONENTS | _PARTICLE_COMPONENTS)
            or resolved in _ADDABLE_BUILTINS
            or resolved in _UI_GETCOMPONENT_TYPES
            or resolved in _TRANSFORM_GETCOMPONENT_TYPES
            or resolved in set(plan.get("addcomponent_types") or [])
            or resolved in gcic
            or bool(_gcic_collector_types(
                resolved, plan, plan.get("mb_bases") or {})))
        if not known:
            out.append(text[i:after])
            i = after
            continue
        include = "0"
        if args:
            a0 = args[0].strip()
            if a0 in ("true", "True", "1"):
                include = "1"
            elif a0 in ("false", "False", "0"):
                include = "0"
            else:
                include = "((%s) ? 1 : 0)" % a0
        recv = m.group("recv")
        go_expr = _gcic_go_expr(recv, cl, plan, locals_ty)
        helper = "GameObject_GetComponentsInChildren_%s" % _c_ident(resolved)
        call = "%s(%s, %s)" % (helper, go_expr, include)
        before = text[i:m.start()]
        # T[] name =  OR  name =
        tm = re.search(
            r"(?:(?:(?:UnityEngine\.)?\w+)\s*\[\s*\]\s*)?(\w+)\s*=\s*$",
            before)
        if tm:
            var = tm.group(1)
            # Drop T[] type if present in the match span
            decl = re.search(
                r"((?:(?:UnityEngine\.)?\w+)\s*\[\s*\]\s*)?(\w+)\s*=\s*$",
                before)
            if decl and decl.group(1):
                out.append(before[:decl.start()])
                out.append("std::vector<int> %s = %s" % (var, call))
            else:
                out.append(before[:tm.start()])
                out.append("%s = %s" % (var, call))
            vector_names.add(var)
            locals_ty[var] = resolved + "[]"
        else:
            out.append(before)
            out.append(call)
        i = after
    text = "".join(out)
    # T[] name = FindObjectsOfType<T>() (lowered already): the same vector
    text = cs2cpp.code_sub(
        r"(?<![\w.])(?:UnityEngine\.)?\w+\s*\[\s*\]\s+(\w+)"
        r"(\s*=\s*Object_FindObjectsOfType_)", r"std::vector<int> \1\2", text)
    # Also catch ref-array fields assigned earlier as Class_field names.
    for f in cl.get("ref_array_fields") or []:
        vector_names.add(f["name"])
        vector_names.add("%s_%s" % (_c_ident(this_class), f["name"]))
    vector_names |= set(re.findall(r"\bstd::vector<int>\s+(\w+)\b", text))
    for name in sorted(vector_names, key=len, reverse=True):
        text = cs2cpp.code_sub(
            r"(?<![.\w])%s\.Length\b" % re.escape(name),
            "%s.size()" % name, text)
    # T elem = vec[i] → int elem = vec[i]
    elem_tys = set(gcic) | set(_GCIC_TYPE_ALIAS.keys()) | set(
        _GCIC_TYPE_ALIAS.values())
    elem_tys |= set(classes) | set(_ADDABLE_BUILTINS) | set(
        _PHYSICS_COMPONENTS) | set(_UI_GETCOMPONENT_TYPES) | set(
        _TRANSFORM_GETCOMPONENT_TYPES)
    for ty in sorted(elem_tys, key=len, reverse=True):
        text = cs2cpp.code_sub(
            r"(?<![\w.])(?:UnityEngine\.)?%s\s+(\w+)\s*=" % re.escape(ty),
            r"int \1 =",
            text)
    return text


def _rewrite_new_gameobject(text):
    """``new GameObject(name)`` → ``GameObject_New(name)`` (``new GameObject()``: named "GameObject"); the extra constructor arguments
    (component types) are dropped.  ``new GameObject("x").AddComponent<T>()`` chains onto the new index as any GameObject does."""
    pat = r"(?<![\w.])new\s+(?:UnityEngine\s*\.\s*)?GameObject\s*\("
    out, pos = [], 0
    for m in list(re.finditer(pat, cs2cpp._blank(text))):
        if m.start() < pos:
            continue
        got = _match_call_args(text, m.end() - 1)
        if not got:
            continue
        args, end = got
        parts = cs2cpp._split_top_level(args) if args.strip() else []
        name = parts[0].strip() if parts and not parts[0].strip().startswith("typeof") else '"GameObject"'
        chain = re.match(r"\s*\.\s*AddComponent\s*<\s*(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)", text[end:])
        if chain:
            out += [text[pos:m.start()], "GameObject_AddComponent_%s(GameObject_New(%s))" % (_c_ident(chain.group(1)), name)]
            pos = end + chain.end()
        else:
            # (a GameObject is its own Transform here: `.transform` / `.gameObject` of the new object is the index)
            tail = re.match(r"\s*\.\s*(?:transform|gameObject)\b", text[end:])
            out += [text[pos:m.start()], "GameObject_New(%s)" % name]
            pos = end + (tail.end() if tail else 0)
    return "".join(out) + text[pos:]


def _rewrite_addcomponent(text, plan, this_class, go_vars=()):
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

    # `go.AddComponent<T>()` on a GameObject variable -- a parameter or a
    # local -- adds to that GameObject, not this one.
    gos = set(go_vars) | set(re.findall(
        r"(?<![\w.])(?:UnityEngine\s*\.\s*)?GameObject\s+(\w+)\s*[=;]",
        cs2cpp._blank(text)))
    # (a GameObject local is an `int` by now: the ones `new GameObject` / Find made are GameObject indices)
    gos |= set(re.findall(r"(?<![\w.])int\s+(\w+)\s*=\s*GameObject_(?:New|Find)\s*\(", cs2cpp._blank(text)))
    gos -= {"this", "gameObject"}
    if gos:
        def repl_var(m):
            if m.group(1):
                locals_ty[m.group(1)] = m.group(4)
            return "%sGameObject_AddComponent_%s(%s)" % (
                ("int %s = " % m.group(1)) if m.group(1) else "",
                _c_ident(m.group(4)), m.group(3))
        text = cs2cpp.code_sub(
            r"(?:(?:(?:UnityEngine\.)?\w+)\s+(\w+)\s*=\s*)?"
            r"(?<![\w.])((%s))\s*\.\s*AddComponent\s*<\s*"
            r"(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)"
            % "|".join(re.escape(g) for g in sorted(gos)),
            repl_var, text)

    # AudioSource asrc = musicSource.gameObject.AddComponent<AudioSource>();
    def repl_typed_go(m):
        var, recv, comp = m.group(1), m.group(2), m.group(3)
        locals_ty[var] = comp
        return "int %s = GameObject_AddComponent_%s(%s)" % (
            var, _c_ident(comp), _go_of_recv(recv))

    text = cs2cpp.code_sub(
        r"(?:(?:UnityEngine\.)?\w+)\s+(\w+)\s*=\s*"
        r"(\w+)\s*\.\s*gameObject\s*\.\s*AddComponent\s*<\s*"
        r"(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)",
        repl_typed_go, text)

    def repl_bare_go(m):
        recv, comp = m.group(1), m.group(2)
        return "GameObject_AddComponent_%s(%s)" % (
            _c_ident(comp), _go_of_recv(recv))

    text = cs2cpp.code_sub(
        r"(\w+)\s*\.\s*gameObject\s*\.\s*AddComponent\s*<\s*"
        r"(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)",
        repl_bare_go, text)

    def repl_typed(m):
        var, comp = m.group(1), m.group(2)
        locals_ty[var] = comp
        return "int %s = GameObject_AddComponent_%s(%s)" % (
            var, _c_ident(comp), go_expr)

    # Camera cam = gameObject.AddComponent<Camera>();
    text = cs2cpp.code_sub(
        r"(?:(?:UnityEngine\.)?\w+)\s+(\w+)\s*=\s*"
        r"(?:(?:this|gameObject)\s*\.\s*)?AddComponent\s*<\s*"
        r"(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)",
        repl_typed, text)

    def repl_bare(m):
        return "GameObject_AddComponent_%s(%s)" % (
            _c_ident(m.group(1)), go_expr)

    text = cs2cpp.code_sub(
        r"(?:(?:this|gameObject)\s*\.\s*)?AddComponent\s*<\s*"
        r"(?:UnityEngine\.)?(\w+)\s*>\s*\(\s*\)",
        repl_bare, text)
    return text, locals_ty



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


# GetComponentsInChildren<Renderer> → SpriteRenderer map (2D authored packs).
_GCIC_TYPE_ALIAS = {
    "Renderer": "SpriteRenderer",
}


def _gcic_resolve_type(tname):
    """Map polymorphic GetComponentsInChildren type → packed GO-map type."""
    return _GCIC_TYPE_ALIAS.get(tname, tname)


def _xf_rows(plan):
    """Transform fileID → the packed (class, inst) that stands for it.

    Several MonoBehaviours on one GameObject each hold a position copy:
    the Rigidbody's row is the one physics moves, else the first class by
    name, as _engine_go_xf picks at runtime."""
    xf_to, xf_rb = {}, set()
    for cname in sorted(plan["classes"]):
        for i, o in enumerate(plan["classes"][cname].get("instances") or []):
            xid = o.get("xf_id")
            if xid is None or str(xid) == "0":
                continue
            rb = bool(o.get("rigidbody2d") or o.get("rigidbody"))
            if str(xid) not in xf_to or (rb and str(xid) not in xf_rb):
                xf_to[str(xid)] = (cname, i)
                if rb:
                    xf_rb.add(str(xid))
    return xf_to


def _attach_transform_parents(plan):
    """Wire authored m_Father → packed parent for live world composition.

    Unity stores localPosition; world = parent_world ∘ local. Pack keeps
    local in instance pos when a packed parent exists. Bodies with their own
    Rigidbody / Rigidbody2D stay independent (Unity simulates them separately).
    UI Images whose Canvas is not a packed body keep baked world `pos`.
    Main Camera under a packed body follows the same rule via Camera_main_pos_*.
    """
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    xf_to = _xf_rows(plan)
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
    # a camera whose own GameObject is packed follows that (_engine_cam_pos)
    own = xf_to.get(str(cam.get("xf_id"))) if cam else None
    plan["camera_follows_parent"] = bool(
        cam and cam.get("xf_parent_class") and not (
            own and _class_has_position(plan["classes"][own[0]])))


def _instance_storage_pos(o):
    """Coords stored in instance arrays: local under a live parent, else world."""
    if o.get("xf_parent_class"):
        lp = o.get("local_pos") or (0.0, 0.0, 0.0)
        return (float(lp[0]), float(lp[1]),
                float(lp[2]) if len(lp) > 2 else 0.0)
    p = o.get("pos") or (0.0, 0.0, 0.0)
    return (float(p[0]), float(p[1]),
            float(p[2]) if len(p) > 2 else 0.0)



def _resolve_transform_field_targets(plan):
    """Authored Transform field refs (fileID) → (class_id, inst) per owner.

    Player.graphicsTrs → Graphics Transform fileID → Graphics instance.
    """
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    xf_to = _xf_rows(plan)
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


def _resolve_go_field_refs(plan):
    """Authored component-reference fields → the GameObject index they name.

    The scene stores a fileID: a Transform / RectTransform's own, a uGUI
    component's, or a GameObject's. All three resolve to the one GO table
    slot, which is what the engine's helpers take. A reference the scene
    leaves empty (or that names something outside the packed tables) is
    null, not GO 0 -- which is another object.
    """
    go_by_id, xf_to_go, _parents = _ui_xf_go_maps(plan)
    comp_to_go = {}
    records = [o for cl in plan["classes"].values()
               for o in cl.get("instances") or []]
    records += list(plan.get("scene_hierarchy") or [])
    for o in records:
        gi = o.get("go_index")
        if gi is None:
            continue
        for fid in (o.get("comp_ids") or []) + (o.get("mb_ids") or []):
            comp_to_go.setdefault(str(fid), int(gi))
    refs = {}
    for cname, cl in plan["classes"].items():
        for f in cl.get("fields") or []:
            if f.get("ty") not in _GO_HANDLE_FIELD_TYPES:
                continue
            row = []
            for o in cl.get("instances") or []:
                fid = str((o.get("object_refs") or {}).get(f["name"]) or "")
                go = -1
                if fid:
                    for table in (xf_to_go, go_by_id, comp_to_go):
                        if fid in table:
                            go = int(table[fid])
                            break
                row.append(go)
            refs[(cname, f["name"])] = row
    plan["go_field_refs"] = refs
    # Authored `T[]` refs: GO indices for handle types, instance indices for
    # a packed class; an empty or unresolved slot is null (-1).
    mb_inst = {}
    for cname, cl in plan["classes"].items():
        for k, o in enumerate(cl.get("instances") or []):
            for fid in o.get("mb_ids") or []:
                mb_inst[(cname, str(fid))] = k
    seeds = {}
    for cname, cl in plan["classes"].items():
        for f in cl.get("ref_array_fields") or []:
            elem = _array_elem_name(f.get("ty") or "")
            rows = []
            for o in cl.get("instances") or []:
                row = []
                for fid in (o.get("object_ref_arrays") or {}).get(
                        f["name"]) or []:
                    if elem in _GO_HANDLE_FIELD_TYPES:
                        row.append(next((int(t[fid]) for t in (
                            xf_to_go, go_by_id, comp_to_go) if fid in t), -1))
                    else:
                        row.append(mb_inst.get((elem, fid), -1))
                rows.append(row)
            if any(rows) and (elem in _GO_HANDLE_FIELD_TYPES
                              or elem in plan["classes"]):
                seeds.setdefault(cname, {})[f["name"]] = rows
    plan["ref_array_seeds"] = seeds
    # and number arrays' authored numbers, seeded by the same function
    nums = {}
    for cname, cl in plan["classes"].items():
        for f in cl.get("list_fields") or []:
            rows = [list((o.get("num_arrays") or {}).get(f["name"]) or [])
                    for o in cl.get("instances") or []] if f.get("array") else []
            if any(rows):
                nums.setdefault(cname, {})[f["name"]] = (
                    _list_elem_c_ty(_list_elem_name(f["ty"]), plan), rows)
                seeds.setdefault(cname, {})
    plan["num_array_seeds"] = nums
    _promote_handle_rot_classes(plan)


def _promote_handle_rot_classes(plan):
    """Rows a script's `Transform` handle reads / writes rotation through get
    live rotation tables (seeded from their authored rotation)."""
    go_cls = {int(o["go_index"]): cname
              for cname, cl in plan["classes"].items()
              for o in cl.get("instances") or []
              if o.get("go_index") is not None}
    live = set(plan.get("live_rot_classes") or [])
    srcs = {}
    for cname, cl in plan["classes"].items():
        try:
            with open(cl.get("script_path") or "", encoding="utf-8",
                      errors="replace") as f:
                srcs[cname] = cs2cpp._blank(f.read())
        except OSError:
            pass
    # ponytail: a field is matched by name in any script (`Lasso.instance.
    # hookTrs.eulerAngles` from Player.cs), so a same-named field elsewhere
    # gets live rotation it may not need -- table space, not behaviour
    rot_names = {n for src in srcs.values() for n in re.findall(
        r"(?<![\w])(\w+)\s*\.\s*(?:rotation|eulerAngles)\b", src)}
    for cname, cl in plan["classes"].items():
        src = srcs.get(cname)
        if src is None:
            continue
        names = set(re.findall(r"(?<![\w.])Transform(?:\[\])?\s+(\w+)\s*[;=,)]",
                               src))
        used = names & rot_names
        if not used:
            continue
        plan["handle_rot"] = True
        gos = set()
        for f in cl.get("fields") or []:
            if f.get("ty") == "Transform":
                gos |= set((plan.get("go_field_refs") or {}).get(
                    (cname, f["name"])) or [])
        for rows in (plan["ref_array_seeds"].get(cname) or {}).values():
            gos |= {g for r in rows for g in r}
        live |= {go_cls[g] for g in gos if g in go_cls}
    plan["live_rot_classes"] = sorted(live)


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
    # receivers: (pattern, owner class C name, field, owner index)
    recvs = [(r"(?<![_\w.])%s" % re.escape(f), idn, f, "i")
             for f in transform_fields]
    classes = plan.get("classes") or {}
    for other in sorted(set(plan.get("singleton_instance_types") or ())
                        & set(classes)):
        oidn = _c_ident(other)
        if other not in text and oidn not in text:
            continue
        for f in classes[other].get("fields") or []:
            if f.get("ty") == "Transform" and not f.get("static"):
                recvs.append((
                    r"(?:(?<![\w.])%s\s*\.\s*[Ii]nstance\s*\.\s*%s\b|"
                    r"(?<![\w.])%s_get_%s\(\s*%s_Instance\(\)\s*\))" % (
                        re.escape(other), re.escape(f["name"]), oidn,
                        re.escape(f["name"]), oidn),
                    oidn, f["name"], "%s_Instance()" % oidn))
    if not recvs:
        return text

    out = []
    i = 0
    while i < len(text):
        found = None
        for pat0, oidn, fname, ix in recvs:
            m = re.search(pat0 + r"\s*\.\s*SetWorldScale\s*\(", text[i:])
            if not m:
                continue
            if found is None or m.start() < found[0]:
                found = (m.start(), m.end(), fname, oidn, ix)
        if not found:
            out.append(text[i:])
            break
        start_rel, end_rel, fname, oidn, ix = found
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
            # `Vector3.one[.SetX(e)]`
            om = re.match(r"(?s)^(?:Vector[23]\s*\.\s*one|Vector2_make\(\s*1\.f"
                          r"\s*,\s*1\.f\s*\))\s*(\.\s*SetX\s*\()?", arg_s)
            if om and not om.group(1) and om.end() == len(arg_s):
                sx, sy, sz = "1.f", "1.f", "1.f"
            elif om and om.group(1):
                ax = _match_call_args(arg_s, om.end() - 1)
                if ax and not arg_s[ax[1]:].strip():
                    sx, sy, sz = ax[0].strip(), "1.f", "1.f"
        if sx is None:
            out.append(text[abs_start:after])
            i = after
            continue
        for vf in vec2_fields:
            sx = cs2cpp.code_sub(r"(?<![_\w])%s\.x\b" % vf,
                        "%s_get_%s_x(i)" % (idn, vf), sx)
            sx = cs2cpp.code_sub(r"(?<![_\w])%s\.y\b" % vf,
                        "%s_get_%s_y(i)" % (idn, vf), sx)
            sz = cs2cpp.code_sub(r"(?<![_\w])%s\.x\b" % vf,
                        "%s_get_%s_x(i)" % (idn, vf), sz)
            sz = cs2cpp.code_sub(r"(?<![_\w])%s\.y\b" % vf,
                        "%s_get_%s_y(i)" % (idn, vf), sz)
        out.append(
            "_engine_set_world_scale("
            "_%s_%s_target_class[%s], (unsigned)_%s_%s_target_inst[%s], "
            "(%s), (%s), (%s))" % (
                oidn, fname, ix, oidn, fname, ix, sx, sy, sz))
        i = after
    text = "".join(out)
    if not transform_fields:
        return text

    # field.localScale = new Vector3(x, y, z): _engine_set_world_scale writes
    # the target's local scale table (the name is SetWorldScale's).
    def _local_scale(m):
        parts = _split_call_args(m.group(2))
        if len(parts) < 2:
            return m.group(0)
        fname = m.group(1)
        return ("_engine_set_world_scale(_%s_%s_target_class[i], "
                "(unsigned)_%s_%s_target_inst[i], (%s), (%s), (%s));" % (
                    idn, fname, idn, fname, parts[0], parts[1],
                    parts[2] if len(parts) > 2 else "1.f"))

    alt = "|".join(re.escape(f) for f in transform_fields)
    text = cs2cpp.code_sub(
        r"(?<![.\w])(%s)\s*\.\s*localScale\s*=\s*new\s+Vector[23]\s*"
        r"\((.*?)\)\s*;" % alt, _local_scale, text, flags=re.DOTALL)

    def target(fname):
        return ("_%s_%s_target_class[i], (unsigned)_%s_%s_target_inst[i]"
                % (idn, fname, idn, fname))
    # any other Vector2 value (`trs.localScale.SetX(..)` once inlined)
    text = cs2cpp.code_sub(
        r"(?<![.\w])(%s)\s*\.\s*localScale\s*=(?!=)\s*([^;]+);" % alt,
        lambda m: "{ Vector2 _up_ls = %s; _engine_set_world_scale(%s, "
                  "_up_ls.x, _up_ls.y, 1.f); }" % (m.group(2).strip(),
                                                   target(m.group(1))),
        text)
    return cs2cpp.code_sub(
        r"(?:\(\s*(%s)\s*\.\s*localScale\s*\)|(?<![.\w])(%s)\s*\.\s*"
        r"localScale)\s*\.\s*([xyz])\b(?!\s*[-+*/]?=(?!=))" % (alt, alt),
        lambda m: "1.f" if m.group(3) == "z" else
        "_engine_get_local_scale(%s, %d)" % (
            target(m.group(1) or m.group(2)), m.group(3) == "y"), text)


def _uses_collision2d_contacts(analyses):
    """Some OnCollision*2D handler reads `coll.GetContact(..)` /
    `coll.contactCount`."""
    for a in analyses or []:
        for c in a.get("classes") or []:
            for m in c.get("methods") or []:
                arg = _collision2d_arg_name(m.get("args") or "")
                if arg and re.search(
                        r"(?<![\w.])%s\s*\.\s*(?:GetContact\s*\(|contactCount\b)"
                        % re.escape(arg), m.get("body") or ""):
                    return True
    return False


def _lower_collision2d_contacts(text, coll):
    """`coll.GetContact(k)` / `coll.contactCount` on the handler's Collision2D."""
    if not coll:
        return text
    text = cs2cpp.code_sub(
        r"(?<![\w.])%s\s*\.\s*GetContact\s*\(" % re.escape(coll),
        "Collision2D_GetContact(%s, " % coll, text)
    return cs2cpp.code_sub(
        r"(?<![\w.])%s\s*\.\s*contactCount\b" % re.escape(coll),
        "Collision2D_contactCount(%s)" % coll, text)


def _rewrite_collision2d_handler_calls(text, cl, plan, coll):
    """`OnCollisionEnter2D(coll)` from another OnCollision*2D handler of the
    same class, passing on its own Collision2D."""
    if not coll:
        return text
    idn = _c_ident(cl["name"])
    methods = [m for _c, m in (plan.get("_methods_by") or {}).get(
        cl["name"], [])] + list(cl.get("methods") or [])
    names = {m["name"] for m in methods
             if m.get("name") in _COLLISION2D_MSGS
             and _collision2d_arg_name(m.get("args") or "")}
    for name in sorted(names):
        text = cs2cpp.code_sub(
            r"(?<![\w.])(?:this\s*\.\s*)?%s\s*\(\s*%s\s*\)"
            % (re.escape(name), re.escape(coll)),
            "%s_%s(i, %s)" % (idn, name, coll), text)
    return text


def _rewrite_vector2_ctor_normalized(text):
    """Members of a ``Vector2_make(a, b)`` value: ``.normalized`` →
    ``Vector2_normalized(..)``, then ``.x`` / ``.y`` → ``Vector2_x(..)``
    (C cannot take a member of a call result in the crust subset)."""
    out, pos = [], 0
    for m in re.finditer(r"(?<![\w.])Vector2_make\s*\(", text):
        if m.start() < pos:
            continue
        got = _match_call_args(text, m.end() - 1)
        if not got:
            continue
        _args, after = got
        expr, end = text[m.start():after], after
        norm = re.match(r"\s*\.\s*normalized\b", text[end:])
        if norm:
            expr, end = "Vector2_normalized(%s)" % expr, end + norm.end()
        axis = re.match(r"\s*\.\s*([xy])\b", text[end:])
        if axis:
            expr, end = "Vector2_%s(%s)" % (axis.group(1), expr), \
                end + axis.end()
        if end == after:
            continue
        out.append(text[pos:m.start()])
        out.append(expr)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _emit_unity_world_xf(p, plan, class_ids):
    """`_engine_world_basis`: an object's world rotation and scale, the 2x2
    matrix R(angle) S(scale) composed up its parents -- Unity's
    parent * (T R S). A child's world position was its parents' positions
    summed: a rotated or scaled parent did not turn or stretch its
    children's offsets (a worm's segments stayed in a straight line however
    its curves turned them). The local angle is the live rotation where the
    class keeps one (`_C_rot_z/w`, animated or turned by a script), else the
    authored one; the scale likewise (`_C_scale_x/y`, else authored)."""
    live_rot = set(plan.get("live_rot_classes") or [])
    live_scale = set(plan.get("live_scale_classes") or [])
    plan["_world_basis"] = True
    p("static void _engine_world_basis(int class_id, unsigned inst, float *b, int depth);")
    for cname in sorted(class_ids):
        cl = plan["classes"][cname]
        if not _class_has_position(cl):
            continue
        idn = _c_ident(cname)
        insts = cl.get("instances") or []
        n = max(1, int(cl.get("n") or len(insts) or 1))
        if cname not in live_rot:
            qs = [tuple((o.get("local_rot") or o.get("rot") or (0, 0, 0, 1))[2:4])
                  for o in insts] + [(0.0, 1.0)] * (n - len(insts))
            if any(abs(q[0]) > 1e-7 for q in qs):
                p("static const float _%s_lq[%d][2] = { %s };" % (idn, n, ", ".join(
                    "{ %rf, %rf }" % (float(q[0]), float(q[1])) for q in qs)))
                cl["_lq"] = True
        if cname not in live_scale:
            ss = [tuple((o.get("local_scale") or (1, 1, 1))[:2]) for o in insts] + \
                 [(1.0, 1.0)] * (n - len(insts))
            if any(abs(v[0] - 1) > 1e-7 or abs(v[1] - 1) > 1e-7 for v in ss):
                p("static const float _%s_ls[%d][2] = { %s };" % (idn, n, ", ".join(
                    "{ %rf, %rf }" % (float(v[0]), float(v[1])) for v in ss)))
                cl["_ls"] = True
    p("static void _engine_world_basis(int class_id, unsigned inst, float *b, int depth) {")
    p("    float qz = 0.f, qw = 1.f, sx = 1.f, sy = 1.f, c, s, l[4];")
    p("    int pc = -1; unsigned pi = 0u;")
    p("    if (depth > 64) { b[0] = 1.f; b[1] = 0.f; b[2] = 0.f; b[3] = 1.f; return; }")
    p("    switch (class_id) {")
    for cname, cid in sorted(class_ids.items(), key=lambda kv: kv[1]):
        cl = plan["classes"][cname]
        if not _class_has_position(cl):
            continue
        idn = _c_ident(cname)
        p("    case %d:" % cid)
        if cname in live_rot:
            p("        qz = _%s_rot_z[inst]; qw = _%s_rot_w[inst];" % (idn, idn))
        elif cl.get("_lq"):
            p("        qz = _%s_lq[inst][0]; qw = _%s_lq[inst][1];" % (idn, idn))
        if cname in live_scale:
            p("        sx = _%s_scale_x[inst]; sy = _%s_scale_y[inst];" % (idn, idn))
        elif cl.get("_ls"):
            p("        sx = _%s_ls[inst][0]; sy = _%s_ls[inst][1];" % (idn, idn))
        p("        pc = _%s_xf_parent_class[inst]; pi = _%s_xf_parent_inst[inst];"
          % (idn, idn))
        p("        break;")
    p("    default: break;")
    p("    }")
    p("    /* the z rotation of quaternion (0, 0, qz, qw): cos and sin of 2 half-angles */")
    p("    c = qw * qw - qz * qz; s = 2.f * qz * qw;")
    p("    l[0] = c * sx; l[1] = -s * sy; l[2] = s * sx; l[3] = c * sy;")
    p("    if (pc < 0) { b[0] = l[0]; b[1] = l[1]; b[2] = l[2]; b[3] = l[3]; return; }")
    p("    { float pb[4]; _engine_world_basis(pc, pi, pb, depth + 1);")
    p("      b[0] = pb[0] * l[0] + pb[1] * l[2]; b[1] = pb[0] * l[1] + pb[1] * l[3];")
    p("      b[2] = pb[2] * l[0] + pb[3] * l[2]; b[3] = pb[2] * l[1] + pb[3] * l[3]; }")
    p("}")
    p("/* a world offset from the parent -> the local position that puts the")
    p("   child there: the parent's world basis inverted (a zero scale has no")
    p("   inverse; the offset is kept as it is) */")
    p("static void _engine_world_to_local(int pc, unsigned pi, float dx, float dy,")
    p("                                   float *lx, float *ly) {")
    p("    float b[4], det;")
    p("    _engine_world_basis(pc, pi, b, 0);")
    p("    det = b[0] * b[3] - b[1] * b[2];")
    p("    if (det > -1e-12f && det < 1e-12f) { *lx = dx; *ly = dy; return; }")
    p("    *lx = (b[3] * dx - b[1] * dy) / det;")
    p("    *ly = (-b[2] * dx + b[0] * dy) / det;")
    p("}")
    p("")


_ENGINE_TRI_TYPEDEF = (
    "typedef struct EngineTri {\n"
    "    float x[3], y[3]; /* world */\n"
    "    float u[3], v[3]; /* the texture's, 0..1 */\n"
    "    float r, g, b, a;\n"
    "    int tex; /* engine_texture_* index; -1 if none */\n"
    "    int sorting_layer, sorting_order; /* as EngineDraw's */\n"
    "} EngineTri;\n")


_WORLD_POS_PROTO = ("static void _engine_world_pos(int class_id, unsigned inst,"
                    " float *x, float *y, float *z, int depth);")


def _rewrite_transform_field_position(text, cl, plan, site=None):
    """``trs.position.x`` on an authored Transform field → the world position
    of the object it references (a missing reference reads 0)."""
    targets = plan.get("transform_field_targets") or {}
    idn = _c_ident(cl["name"])
    for f in cl.get("fields") or []:
        fname = f.get("name")
        if f.get("ty") != "Transform" or (cl["name"], fname) not in targets:
            continue
        tgt = ("_%s_%s_target_class[i], (unsigned)_%s_%s_target_inst[i]"
               % (idn, fname, idn, fname))
        if plan.get("has_transform_parents"):
            # a whole Vector2 written (or added): the world setter, z kept;
            # a `new Vector3(x, y, z)`: its z (`t.position.z` reads the kept one)
            def world_set(m, t=tgt):
                rhs, add = m.group(2).strip(), m.group(1)
                z = "_up_wz"
                inner = rhs
                while inner.startswith("(") and (cs2cpp.match_call_args(
                        inner, 0) or (0, 0))[1] == len(inner):
                    inner = inner[1:-1].strip()
                v3 = re.match(r"new\s+Vector3\s*(?=\()", inner)
                got = cs2cpp.match_call_args(inner, v3.end()) if v3 else None
                xyz = cs2cpp._split_top_level(got[0]) if got and got[1] == len(
                    inner) else []
                if len(xyz) == 3:
                    rhs = "Vector2_make(%s, %s)" % (xyz[0], xyz[1])
                    z = "%s(%s)" % ("_up_wz + " if add else "", xyz[2].strip())
                return ("{ Vector2 _up_tp = %s; float _up_wx, _up_wy, _up_wz; "
                        "_engine_world_pos(%s, &_up_wx, &_up_wy, &_up_wz, 0); "
                        "_engine_set_world(%s, %s_up_tp.x, %s_up_tp.y, %s); }"
                        % (rhs, t, t, "_up_wx + " if add else "",
                           "_up_wy + " if add else "", z))
            text = cs2cpp.code_sub(
                r"(?<![\w.])(?:this\s*\.\s*)?%s\s*\.\s*position\s*(\+?)=(?!=)"
                r"\s*([^;]+);" % re.escape(fname), world_set, text)
            if site is not None and "_up_tp" in text:
                site.setdefault("protos", set()).add(_WORLD_POS_PROTO)
        pat = (r"(?<![\w.])(?:this\s*\.\s*)?%s\s*\.\s*position\s*\.\s*([xyz])\b"
               r"(?!\s*(?:=[^=]|\+=|-=|\*=|/=|\+\+|--))" % re.escape(fname))
        if not re.search(pat, text):
            continue
        if site is not None:
            site.setdefault("protos", set()).add(_WORLD_POS_PROTO)
        text = cs2cpp.code_sub(
            pat,
            lambda m, fn=fname: (
                "({ float _up_wx, _up_wy, _up_wz; "
                "_engine_world_pos(_%s_%s_target_class[i], "
                "(unsigned)_%s_%s_target_inst[i], "
                "&_up_wx, &_up_wy, &_up_wz, 0); _up_w%s; })"
                % (idn, fn, idn, fn, m.group(1))),
            text)
    return text


def _class_id(plan, cname):
    """The packed class's id — the tag `_engine_iref` and the ticks use."""
    return {n: i for i, n in enumerate(sorted(plan.get("classes") or ()))}.get(
        cname, -1)


def _named_ref_class(name, cl, plan, text):
    """The packed class a body's name holds an instance of, or None.

    A handle field of `cl` (`DragUpdater dragUpdater`), then a local
    declared with a packed type (`Cosmetic c = ..`).
    """
    for fname, _ty, _bits, kind in cl.get("members") or []:
        if fname == name and str(kind).startswith("idx:"):
            other = kind.split(":", 1)[1]
            if other in (plan.get("classes") or {}):
                return other
    for f in cl.get("fields") or []:
        if f.get("name") == name and f.get("ty") in (plan.get("classes") or {}):
            return f["ty"]
    m = re.search(r"(?<![\w.])([A-Z][\w.]*)\s+%s\s*=" % re.escape(name),
                  cs2cpp._blank(text))
    if m:
        ty = m.group(1).split(".")[-1]
        if ty in (plan.get("classes") or {}):
            return ty
    return None


def _rewrite_static_ref_arrays(text, cl, plan):
    """Static `T[]` fields as their packed vector — Extensions, reads, length.

    The authored ``GameManager.updatables = GameManager.updatables.Add(this)``
    copies the array to add one element (`Extensions.CollectionExtensions`);
    the pack keeps the one vector the copy was building and appends to it.
    ``Remove`` erases the first match, as `List.Remove` does.

    A `T[]` of a packed class holds instance indices, so its element is the
    index the body already names. An interface's holds `_engine_iref`: the
    implementors are separate packed arrays, and an element has to say
    which class before it says which instance.
    """
    arrays = _any_static_ref_arrays(plan)
    if not arrays:
        return text
    this_name = cl.get("name")
    bases = plan.get("mb_bases") or {}
    # Every qualified `Other.field` before any bare name, so this class's
    # own `instances` does not match the tail of another's.
    targets = []
    for cname, fname, elem, iface in arrays:
        mangled = "%s_%s" % (_c_ident(cname), fname)
        targets.append((
            r"(?<![\w.])%s\s*\.\s*%s\b" % (re.escape(cname), re.escape(fname)),
            mangled, elem, iface))
    for cname, fname, elem, iface in arrays:
        if cname == this_name:
            targets.append((r"(?<![\w.])%s\b" % re.escape(fname),
                            "%s_%s" % (_c_ident(cname), fname), elem, iface))

    def element(arg):
        """(class, index expression) for the ref an argument names."""
        arg = arg.strip()
        if arg == "i":
            return this_name, "i"
        other = _named_ref_class(arg, cl, plan, text)
        return (other, arg) if other else (None, None)

    for pat, mangled, elem, iface in targets:
        for method in ("Add", "Remove"):
            while True:
                m = re.search(
                    pat + r"\s*=\s*" + pat + r"\s*\.\s*%s\s*\(" % method,
                    cs2cpp._blank(text))
                if not m:
                    break
                parsed = _match_call_args(text, m.end() - 1)
                if not parsed:
                    break
                args_str, after = parsed
                end = after
                while end < len(text) and text[end] in " \t":
                    end += 1
                if end < len(text) and text[end] == ";":
                    end += 1
                ocname, expr = element(args_str)
                if ocname is None or (
                        iface and not _mb_is_a(ocname, elem, bases)) or (
                        not iface and ocname != elem):
                    break
                if iface:
                    call = "_engine_iref_%s(%s, %d, %s);" % (
                        "push" if method == "Add" else "erase",
                        mangled, _class_id(plan, ocname), expr)
                elif method == "Add":
                    call = "%s.push_back(%s);" % (mangled, expr)
                else:
                    call = "_engine_ref_erase(%s, %s);" % (mangled, expr)
                text = text[:m.start()] + call + text[end:]
    for pat, mangled, elem, _iface in targets:
        # `= new T[0]`: a new empty array; the vector is the only copy
        text = cs2cpp.code_sub(
            pat + r"\s*=(?!=)\s*new\s+%s\s*\[\s*0\s*\]\s*;" % re.escape(elem),
            "%s.clear();" % mangled, text)
        text = cs2cpp.code_sub(pat + r"\s*\.\s*Length\b",
                               "%s.size()" % mangled, text)
        text = cs2cpp.code_sub(pat, mangled, text)
    return text



def _nre_at_expr(site, line):
    """C expr that raises Unity-style NullReferenceException at *line*."""
    return (
        "_engine_null_reference_at(%s, %s, %s, %d, 0)"
        % (_c_string(site["class"]), _c_string(site["method"]),
           _c_string(site["path"]), int(line))
    )


def _cs_qualified_name(file_text, cls):
    """*cls* as Unity's stack trace names it: within its namespaces
    (`SlimeJump.ComplexTimer`)."""
    scan = cs2cpp._blank(file_text or "")
    cm = re.search(r"\b(?:class|struct)\s+%s\b" % re.escape(cls), scan)
    if not cm:
        return cls
    names = []
    for nm in re.finditer(r"\bnamespace\s+([\w.]+)\s*(\{|;)", scan):
        if nm.start() > cm.start():
            break
        close = (_match_close(scan, nm.end() - 1, "{", "}")
                 if nm.group(2) == "{" else len(scan))
        if close is not None and close > cm.start():
            names.append(nm.group(1))
    return ".".join(names + [cls])


def _emit_frame_wrapper(p, lines, hdr, sym, rty, site, m, coll_param):
    """A lowered method's body becomes `sym__b`; `sym` pushes its stack
    frame around it, so a script exception prints Unity's trace."""
    head = lines[hdr]
    lines[hdr] = head.replace(" %s(" % sym, " %s__b(" % sym, 1)
    params = head[head.index("(") + 1:head.rindex(")")]
    args = [] if m.get("static") else ["i"]
    args += [coll_param] if coll_param else [
        prm.name for prm in cs2cpp.parse_params(m.get("args") or "")]
    name = m["name"]
    cls = m.get("inherited_from") or site["class"]
    bm = re.match(r"base__(\w+?)__(\w+)$", name)
    if bm:
        cls, name = bm.group(1), bm.group(2)
    p(head)
    if rty and rty != "void":
        p("    %s _r;" % rty)
    p("    _engine_push(%s, %s, %s);" % (
        _c_string(_cs_qualified_name(site.get("file_text"), cls)),
        _c_string(name), _c_string(site.get("path") or "")))
    call = "%s__b(%s)" % (sym, ", ".join(args) if params.strip() != "void" else "")
    p("    %s;" % (("_r = " + call) if rty and rty != "void" else call))
    p("    _engine_depth = _engine_depth - 1;")
    if rty and rty != "void":
        p("    return _r;")


def _nre_line_sites(line, site, ln, syms):
    """One emitted body line (C# line *ln*: the lowering keeps lines): an
    instance accessor in *syms* called on a receiver other than `i` checks
    it for null first (`_engine_nn`), and every NRE on the line reports
    script, line and column. The column is the receiver's in the C# line
    (`affectedByVortex` of `affectedByVortex.velocity.x`), else the
    statement's first character.

    ponytail: a line the lowering moved reports the line it landed on."""
    src = (site.get("file_text") or "").split("\n")
    cs = src[ln - 1] if 0 < ln <= len(src) else ""
    first = len(cs) - len(cs.lstrip()) + 1

    def col_of(member):
        for mem in (member, re.sub(r"_[xyz]$", "", member)):
            for m in re.finditer(r"((?:[A-Za-z_]\w*\s*(?:\(\s*\))?\s*\.\s*)+)"
                                 r"%s\b" % re.escape(mem), cs):
                return m.start(1) + 1
        return first

    head = "%s, %s, %s, %d" % (_c_string(site["class"]),
                               _c_string(site["method"]),
                               _c_string(site.get("path") or "<cs>"), ln)
    # for `_site_stops`, once every helper is emitted
    line = "%s /*@site %s @cs %s*/" % (line, head, cs.replace("*/", "* /"))
    line = re.sub(r"_engine_null_reference_at\(\"[^\"]*\", \"[^\"]*\", "
                  r"\"[^\"]*\", -?\d+, 0\)",
                  lambda m: "_engine_null_reference_at(%s, %d)" % (head, first),
                  line)
    calls = [m for m in re.finditer(r"(?<![\w.])(\w+)\(", cs2cpp._blank(line))
             if m.group(1) in syms]
    for m in reversed(calls):
        scan = cs2cpp._blank(line)
        close = _match_close(scan, m.end() - 1, "(", ")")
        if close is None:
            continue
        args = cs2cpp.split_call_args(line[m.end():close])
        if not args or args[0].strip() == "i":
            continue
        a = m.end() + line[m.end():close].index(args[0])
        b = a + len(args[0])
        line = "%s_engine_nn(%s, %s, %d)%s" % (
            line[:a], args[0], head, col_of(syms[m.group(1)]), line[b:])
    return line


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
            if fl in ("velocity", "linearvelocity") and not axis:
                return ("({ int _up_rb = %s; "
                        "_up_rb < 0 "
                        "? (%s, Vector2_make(0.f, 0.f)) "
                        ": Vector2_make(_Rigidbody2D_vel_x[_up_rb], "
                        "_Rigidbody2D_vel_y[_up_rb]); })"
                        % (get, nre))
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
                or comp in (plan.get("interfaces") or {})
                or comp in _PHYSICS_COMPONENTS or comp in _COLLIDER2D_TYPES
                or comp in (_JOINT2D_COMPONENTS | _PARTICLE_COMPONENTS)
                or comp == "LineRenderer"
                or comp in _ADDABLE_BUILTINS
                or comp in _UI_GETCOMPONENT_TYPES
                or comp in _TRANSFORM_GETCOMPONENT_TYPES
                or comp in set(plan.get("addcomponent_types") or []))

    def _raise_unknown_comp(ch, comp):
        idx = ch.get("type_idx")
        if idx is None:
            idx = ch.get("start") or 0
        _raise_cs_at_site(site, idx, "CS0246", _CS0246 % comp)

    def _in_parent(ch, repl):
        if not ch.get("in_parent"):
            return repl
        return repl.replace("GameObject_GetComponent_",
                            "GameObject_GetComponentInParent_")

    for ch in chains:
        comp = ch.get("component")
        if comp in (plan.get("interfaces") or {}) and (
                ch.get("field") or not _iface_implementors(comp, plan)):
            continue                  # left as C#: the method stubs (CS8000)
        if comp in _RENDERER_LOOKUP_TYPES and not _known_component(comp):
            continue
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
            text = text[:ch["start"]] + _in_parent(ch, repl) + text[ch["end"]:]
            continue

        find_args = ch.get("find_args") or ""
        go_expr = "GameObject_Find(%s)" % find_args
        if ch.get("recv"):
            # newGo.GetComponent<T>() — recv is a GO / component index.
            recv = ch["recv"]
            if recv in ("gameObject", "this"):
                go_expr = "_engine_go_of_%s(i)" % _c_ident(this_class)
            else:
                go_expr = recv
            if not comp:
                repl = go_expr
            else:
                if not _known_component(comp):
                    _raise_unknown_comp(ch, comp)
                if field:
                    repl = _field_after_get(
                        comp, field, go_expr, line, axis, body_idx)
                else:
                    repl = "GameObject_GetComponent_%s(%s)" % (
                        _c_ident(comp), go_expr)
            text = text[:ch["start"]] + _in_parent(ch, repl) + text[ch["end"]:]
            continue
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
        text = text[:ch["start"]] + _in_parent(ch, repl) + text[ch["end"]:]
    return text


# `public int Hp { get; private set; }`, `static Foo Instance { get; private set; }`, `float Speed { get; set; } = 2f;`: an
# auto-property is a field with an access rule.  Nothing in a packed script can tell them apart (no reflection, no
# interface or override on one), but the packer gave a property no storage, so every method that read one was
# "not lowered yet" and the managed class (--hybrid) had no member of that name.  They are fields from here on,
# one line to one line, so diagnostics keep their line numbers.  A property with a body (`get { .. }`), an abstract /
# virtual / override / extern / `new` one and an interface member are left alone.
_AUTO_PROPERTY = re.compile(
    r"(?m)^([ \t]*)((?:(?:public|private|protected|internal)[ \t]+)?(?:static[ \t]+)?)"
    r"([A-Za-z_][\w.]*(?:<[^;{}()=]*>)?(?:\[[,\s]*\])*\??)[ \t]+([A-Za-z_]\w*)[ \t]*"
    r"\{[ \t]*get[ \t]*;[ \t]*(?:(?:private|protected|internal)[ \t]+)?(?:set[ \t]*;[ \t]*)?\}"
    r"([ \t]*=[^;\n]+;)?")
_NOT_A_TYPE = frozenset(("return", "new", "override", "abstract", "virtual", "extern", "sealed", "readonly", "const",
                         "else", "case", "throw", "yield", "await", "in", "out", "ref"))


def _auto_properties_to_fields(text):
    scan = cs2cpp._blank(text)
    edits = []
    for m in _AUTO_PROPERTY.finditer(scan):
        if m.group(3) in _NOT_A_TYPE:
            continue
        indent, access, ty, name, init = (text[m.start(i):m.end(i)] if m.group(i) is not None else ""
                                          for i in (1, 2, 3, 4, 5))
        if init:
            init = " =" + init.split("=", 1)[1]
        else:
            init = ";"
        edits.append((m.start(), m.end(), "%s%s%s %s%s" % (indent, access, ty, name, init)))
    for a, b, new in reversed(edits):
        text = text[:a] + new + text[b:]
    return text


def analyze_script(path, text=None, shallow=False):
    """Fields, methods, Unity API used, whether the script spawns.

    *shallow*: fields / type only (no method bodies). Used for GetComponent
    targets pulled in by reference so vendor APIs inside Fracture() etc. do
    not refuse the pack — instances and live GO maps still pack.
    """
    if text is None:
        text = _read(path)
    # Player pack: editor-only regions are not code.
    text = _blank_unity_editor_regions(text)
    text = _auto_properties_to_fields(text)
    if shallow:
        text = _blank_method_bodies(text)
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
    getcomponentsinchildren_types = set()
    for ch in _ast_find_getcomponent_chains(text):
        if ch.get("find_args") is not None and not ch.get("on_this"):
            apis.add("GameObject.Find")
        if ch.get("component"):
            apis.add("GetComponent")
            getcomponent_types.add(ch["component"])
            if ch["component"] in _PHYSICS_COMPONENTS:
                apis.add(ch["component"])
            if ch.get("in_parent"):
                apis.add("GetComponentInParent<%s>" % ch["component"])
                apis.add("transform.parent")
        elif ch.get("on_this"):
            apis.add("GetComponent")
    # `h.GetChild(k).GetComponent<Image>()` (`_rewrite_get_child`)
    for m in re.finditer(r"\.\s*GetChild\s*\([^;]*?\)\s*\.\s*GetComponent\s*<\s*"
                         r"(?:UnityEngine\.UI\.)?(\w+)\s*>", scan):
        if m.group(1) in _UI_GETCOMPONENT_TYPES:
            apis.add("GetComponent")
            getcomponent_types.add(m.group(1))
    for m in re.finditer(
            r"GetComponentsInChildren\s*<\s*(?:UnityEngine\.)?(\w+)\s*>",
            scan):
        raw = m.group(1)
        resolved = _gcic_resolve_type(raw)
        getcomponentsinchildren_types.add(resolved)
        getcomponent_types.add(resolved)
        apis.add("GetComponentsInChildren")
        # Need live parent walk for the subtree.
        apis.add("transform.parent")
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
    # (any `x.color =`, the renderer maybe another script's field: the live
    # table costs only memory when x is something else)
    if re.search(r"\.\s*color\s*=(?!=)", scan):
        apis.add("SpriteRenderer.color")
    if re.search(r"\bSpriteRenderer\b", scan) and re.search(
            r"(?<![\w.])\w+\s*\.\s*enabled\s*=(?!=)", scan):
        apis.add("SpriteRenderer.enabled")
    if re.search(r"\.\s*simulated\s*=(?!=)", scan):
        apis.add("Rigidbody2D.simulated")
    if re.search(r"\.\s*worldCenterOfMass\b", scan):
        apis.add("Rigidbody2D.worldCenterOfMass")
    if re.search(r"(?<![\w.])Sprite\b", scan) and re.search(
            r"\.\s*sprite\b", scan):
        apis.add("Renderer.sprite")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
                 r"(?:eulerAngles\b|Translate\s*\()", scan):
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
    if re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?rectTransform\s*\.\s*localScale\b",
            scan):
        apis.add("transform.localScale")
    if (re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?rectTransform\s*\.\s*"
            r"anchoredPosition\b",
            scan)
            or re.search(
                r"GetComponent\s*<\s*(?:UnityEngine\.)?RectTransform\s*>\s*"
                r"(?:\([^)]*\))?\s*\.\s*anchoredPosition\b",
                scan)
            or re.search(
                r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
                r"anchoredPosition\b",
                scan)):
        apis.add("rectTransform.anchoredPosition")
    if (re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?rectTransform\s*\.\s*sizeDelta\b",
            scan)
            or re.search(
                r"GetComponent\s*<\s*(?:UnityEngine\.)?RectTransform\s*>\s*"
                r"(?:\([^)]*\))?\s*\.\s*sizeDelta\b",
                scan)
            or re.search(
                r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*sizeDelta\b",
                scan)):
        apis.add("rectTransform.sizeDelta")
    # `transform.parent`, and `handleTrs.parent` on a component field: both
    # walk the same live parent table.
    if re.search(r"(?<![.\w])\w+\s*\.\s*parent\b", scan):
        apis.add("transform.parent")
    if re.search(r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*gameObject\b",
                 scan):
        apis.add("transform.gameObject")
    if re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*SetParent\s*\(",
            scan):
        apis.add("transform.SetParent")
    if re.search(r"(?<![\w.])(?:this\s*\.\s*)?gameObject\s*\.\s*SetActive\s*\(",
                 scan):
        apis.add("GameObject.SetActive")
    if re.search(r"\.\s*(?:reactionForce|GetReactionForce|linearOffset|target)\b",
                 scan) and re.search(r"Joint2D\b", scan):
        apis.add("Vector2")
    # `go.transform.parent = ..`: SetParent through a GameObject
    if re.search(r"(?<![\w.])\w+\s*\.\s*transform\s*\.\s*parent\s*=(?!=)", scan):
        apis.add("transform.SetParent")
    # `name` / `gameObject.name`: the GameObject name table (a local of that
    # name only costs the table)
    if re.search(r"(?<![\w.])(?:this\s*\.\s*|gameObject\s*\.\s*)?name\b(?!\s*\()",
                 scan):
        apis.add("MonoBehaviour.name")
    # a position used as a value, or a Vector3 function a 2D pack lowers
    # through Vector2 (_lower_position_values): Vector2 values appear
    if re.search(r"(?<![\w.])(?:this\s*\.\s*)?\w+(?:\s*\[[^\[\]]*\])?\s*\.\s*position\b"
                 r"(?!\s*\.)(?!\s*[-+*/]?=(?!=))", scan) or re.search(
                     r"(?<![\w.])Vector[23]\s*\.\s*(?:Lerp|LerpUnclamped|"
                     r"MoveTowards|Min|Max)\s*\(", scan) or re.search(
                     r"(?<![\w.])(?:this\s*\.\s*)?transform\s*\.\s*position\s*"
                     r"=(?!=)\s*(?!new\b)", scan):
        apis.add("Vector2")
    # Physics2D queries: Box2D-Packed's, over a Vector2; a hit's collider is
    # read for its GameObject.
    if re.search(r"\bPhysics2D\s*\.\s*(?:(?:Raycast|OverlapCircle|OverlapPoint)"
                 r"(?:All)?|Linecast)\s*\(|\bPhysics2D\s*\.\s*"
                 r"queriesStartInColliders\b", scan):
        apis.add("Physics2D.query")
        apis.add("Vector2")
        apis.add("GameObject.SetActive")
    # Collider2D.IsTouchingLayers: the last step's contact and trigger
    # pairs, by the other collider's GameObject layer
    if re.search(r"\.\s*IsTouchingLayers\s*\(", scan):
        apis.add("Collider2D.IsTouchingLayers")
        apis.add("GameObject.SetActive")
    if re.search(r"\bVector2Int\b", scan):
        apis.add("Vector2Int")
    # The Input System's Gamepad.current: the host's gamepad.
    if re.search(r"(?<![\w.])Gamepad\s*\.\s*(?:current|all)\b", text):
        apis.add("Gamepad.current")
        apis.add("Vector2")
    # The mouse wheel: the host's scroll, per frame.
    if re.search(r"Input\s*\.\s*mouseScrollDelta|Mouse ScrollWheel|"
                 r"Mouse\s*\.\s*current\s*\.\s*scroll", text):
        apis.add("Input.mouseScrollDelta")
        apis.add("Vector2")
    # A collision / trigger handler reads the other's GameObject from them.
    if re.search(r"\bOn(?:Trigger|Collision)(?:Enter|Stay|Exit)2D\s*\(", scan):
        apis.add("GameObject.SetActive")
    # Reading activeSelf / activeInHierarchy needs the same active tables;
    # `gameObject.layer`, the authored layer table.
    if re.search(r"\.\s*active(?:Self|InHierarchy)\b|"
                 r"(?<![\w.])gameObject\s*\.\s*layer\b", scan):
        apis.add("GameObject.SetActive")
    # transform.parent.gameObject.SetActive / anyRecv.gameObject.SetActive
    if re.search(r"\.\s*gameObject\s*\.\s*SetActive\s*\(", scan):
        apis.add("GameObject.SetActive")
        apis.add("transform.gameObject")
    # foo.transform.SetParent / trs.SetParent (Transform receiver).
    if re.search(r"\.\s*transform\s*\.\s*SetParent\s*\(", scan):
        apis.add("transform.SetParent")
    if re.search(r"(?<![.\w])\w+\s*\.\s*SetParent\s*\(", scan):
        apis.add("transform.SetParent")
    # DetachChildren is SetParent(null) per child; a Transform handle's
    # position / rotation walks the same live hierarchy tables.
    trs_names = set(re.findall(r"(?<![\w.])Transform\s+(\w+)\s*[;=,)]", scan))
    if re.search(r"(?<![.\w])\w+\s*\.\s*DetachChildren\s*\(", scan) or any(
            re.search(r"(?<![.\w])%s\s*\.\s*(?:position\b(?!\s*\.\s*[xyz]\b)"
                      r"|rotation\b|eulerAngles\b)" % re.escape(n), scan)
            for n in trs_names) or re.search(
                # another script's handle through its singleton
                r"\.\s*[Ii]nstance\s*\.\s*\w+\s*\.\s*(?:rotation|eulerAngles)\b",
                scan):
        apis.add("transform.SetParent")
    if re.search(
            r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*GetSiblingIndex\s*\(",
            scan):
        apis.add("transform.GetSiblingIndex")
    if re.search(r"\.\s*transform\s*\.\s*GetSiblingIndex\s*\(", scan):
        apis.add("transform.GetSiblingIndex")
    if re.search(r"(?<![.\w])\w+\s*\.\s*GetSiblingIndex\s*\(", scan):
        apis.add("transform.GetSiblingIndex")
    if re.search(r"\.\s*(?:childCount\b|GetChild\s*\()", scan):
        apis.update(("transform.GetChild", "transform.GetSiblingIndex",
                     "transform.parent"))
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
    # ForceUpdateCanvases is lowered to a no-op (not invent).
    if (re.search(r"AddComponent\s*<\s*(?:UnityEngine\.)?Canvas\s*>", scan)
            or re.search(r"typeof\s*\(\s*Canvas\s*\)", scan)):
        apis.add("Canvas")
    if re.search(r"Canvas\.ForceUpdateCanvases\b", scan):
        apis.add("Canvas.ForceUpdateCanvases")
    if re.search(r"(?<![\w.])(?:UnityEngine\.)?Rect\s*\.\s*"
                 r"PointToNormalized\s*\(", scan):
        apis.add("Rect.PointToNormalized")
    # `trs.rect.height` — the RectTransform's own rect, not `Camera.rect`.
    if re.search(r"(?<![\w.])\w+\s*\.\s*rect\s*\.\s*"
                 r"(?:width|height|min|max|center|size|x|y)\b", scan):
        apis.add("RectTransform.rect")
    if re.search(r"(?<![\w.])GetWorldRect\s*\(\s*\)", scan):
        apis.add("Extensions.GetWorldRect")
    if re.search(r"(?<![\w.])Camera\s*\.\s*main\s*\.\s*"
                 r"ScreenToWorldPoint\s*\(", scan):
        apis.add("Camera.main.ScreenToWorldPoint")
    if re.search(r"(?<![\w.])Rect\b|\.\s*rect\b", scan):
        apis.add("Rect")
    if re.search(r"(?<![\w.])Mouse\s*\.\s*current\s*\.\s*position\b", scan):
        apis.add("Mouse.current.position")
    if re.search(r"\bInputAction\b", scan):
        apis.add("InputAction")
    if re.search(r"(?<![\w.])SceneManager\s*\.", scan):
        apis.add("SceneManager")
    if re.search(
            r"InputManager\s*\.\s*Using(?:Gamepad|Keyboard|Mouse|Phone)\b",
            scan):
        apis.add("InputManager.Using")
    if re.search(
            r"(?<![\w.])(?:System\.Collections\.Generic\.)?List\s*<",
            scan):
        apis.add("List")
    if re.search(
            r"(?<![\w.])(?:System\.Collections\.Generic\.)?Dictionary\s*<",
            scan):
        apis.add("Dictionary")
    if re.search(
            r"(?<![\w.])(?:System\.Collections\.Generic\.)?SortedList\s*<",
            scan):
        apis.add("SortedList")
    findobject_types = set()
    singleton_instance_types = set()
    for m in re.finditer(
            r"(?:UnityEngine\.)?(?:Object\.)?FindObjectOfType\s*<\s*(\w+)\s*>",
            scan):
        apis.add("FindObjectOfType")
        findobject_types.add(m.group(1))
    for m in re.finditer(
            r"(?:UnityEngine\.)?(?:Object\.)?FindObjectsOfType\s*<\s*(\w+)\s*>",
            scan):
        apis.add("FindObjectOfType")
        apis.add("FindObjectsOfType<%s>" % m.group(1))
        findobject_types.add(m.group(1))
    for m in re.finditer(
            r"(?<![\w.])(\w+)\s*\.\s*(?:Instance|instance)\b", scan):
        # CosmeticsMenu.Instance — not foo.instance unless type-like name.
        tname = m.group(1)
        if tname.lstrip("_")[:1].isupper():
            apis.add("Singleton.Instance")
            singleton_instance_types.add(tname)
            findobject_types.add(tname)  # Instance getter needs FindObjectOfType
    # `static Own Instance;` declared by a class of this file: a singleton
    # *field* -- null until assigned, not found (see the getter). Only a
    # `Type.Instance` use from elsewhere used to make it a singleton, and its
    # own `Instance = this` then reached C as `Type_Instance() = i`.
    singleton_field_types = set()
    own_types = set(re.findall(r"\bclass\s+(\w+)", scan))
    for m in re.finditer(r"\bstatic\s+(?:readonly\s+)?(\w+)\s+([Ii]nstance)\s*[;=]",
                         scan):
        if m.group(1) in own_types:
            apis.add("Singleton.Instance")
            singleton_instance_types.add(m.group(1))
            singleton_field_types.add(m.group(1))
            findobject_types.add(m.group(1))
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
    if re.search(r"(?:UnityEngine\.)?Debug\.Log(?:Warning|Error)?\s*\(", scan):
        apis.add("Debug.Log")
    if re.search(r"(?<![\w.])Cursor\s*\.\s*visible\b", scan):
        apis.add("Cursor.visible")
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
    if re.search(
            r"(?<![\w.])(?:(?:UnityEngine\.)?Object\.)?Instantiate\s*"
            r"\(\s*(?:this\s*,|[\w.]+\s*\.\s*GetChild\s*\()",
            scan):
        apis.add("Instantiate.parent")
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
    # C# string + value must not become C pointer arithmetic. A literal on
    # either side: `name + " hp"` (the literal second) was missed, and its
    # concatenation helper never emitted -- `undeclared '_str_plus_s'`.
    if re.search(
            r'"\s*\+|\+\s*"|'
            r"(?<![\w])Application\.(?:dataPath|persistentDataPath|"
            r"productName)\s*\+",
            scan):
        apis.add("string.+")
    # A method returning a string copies it to a scratch slot.
    if re.search(r"(?m)^[ \t]*(?:(?:public|private|protected|internal|static|"
                 r"override|virtual)\s+)*string\s+\w+\s*\(", scan):
        apis.add("string.+")
    # A Stack / Queue take checks emptiness with a runtime helper, and a
    # List search is a helper per element type: both are emitted with the
    # string helpers.
    if "_cs_require_nonempty(" in scan or "_cs_idx" in scan or \
            "_cs_ll_" in scan or "_cs_bytes_" in scan or "_cs_file_" in scan or \
            "_cs_switch_str(" in scan or (
            re.search(r"\bList\s*<", scan) and re.search(
                r"\.\s*(?:RemoveAt|Insert|Contains|IndexOf|Remove)\s*\(",
                scan)):
        apis.add("string.+")
    # StringBuilder appends formatted values from the scratch slots, and
    # JsonUtility's text is built the same way.
    if re.search(r"\bStringBuilder\b|\bJsonUtility\b|\bStopwatch\b|"
                 r"\bDateTime\b", scan):
        apis.add("string.+")
    if re.search(r"\bJsonUtility\s*\.\s*FromJsonOverwrite\b", scan):
        apis.add("JsonUtility.FromJsonOverwrite")
    # The runtime table's APIs are helpers emitted with the string ones.
    if runtime.API_RE.search(scan):
        apis.add("string.+")
        if re.search(r"\bTime\s*\.\s*(?:realtimeSinceStartup|unscaledTime\b|"
                     r"timeSinceLevelLoad)", scan):
            apis.add("Time.time")
        if re.search(r"\bRandom\s*\.\s*insideUnitCircle\b", scan):
            apis.add("Vector2")
    # A string compared with null is lowered to a string helper, which
    # lives with the scratch slots.
    if re.search(r"(?<![\w.])string\b", scan) and re.search(
            r"[=!]=\s*null\b|\bnull\s*[=!]=", scan):
        apis.add("string.+")
    # `s == "lit"` / `"lit" != s` is lowered to `_cs_str_Equals`, which is
    # emitted with them as well (it was undeclared when nothing else in the
    # project used a string helper).
    if re.search(r'[=!]=\s*"|"\s*[=!]=', scan):
        apis.add("string.+")
    # Formatting ($"..", string.Format, ToString) builds strings there too.
    if re.search(r'(?<![\w@])\$"|\bFormat\s*\(|\.\s*ToString\s*\(', scan):
        apis.add("string.+")
    # String members (`s.ToUpper()`) return strings in scratch slots too.
    if re.search(r"\.\s*(?:%s)\b|(?<![\w.])(?:string|String)\s*\.\s*(?:%s)\b"
                 % ("|".join(_CS_STRING_MEMBER_NAMES),
                    "|".join(_CS_STRING_STATIC_NAMES)), scan) and \
            re.search(r"(?<![\w.])string\b|\"", scan):
        apis.add("string.+")
    # ... nor a concatenation that starts from a string variable
    # (`s = s + "x"`, `s += n`), which cs2cpp now lowers too.
    str_names = set(re.findall(r"(?<![\w.])string\s+(\w+)", scan))
    if str_names and re.search(
            r"(?<![\w.])(?:%s)\s*\+(?!\+)" % "|".join(
                re.escape(n) for n in sorted(str_names)), scan):
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
    # `Translate` reads the rotation (Space.Self turns the delta by it; see
    # _lower_translate): the class keeps a live rotation, as for Rotate.
    writes_rot = bool(re.search(
        r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
        r"(?:Rotate|LookAt|Translate)\s*\(", scan) or re.search(
        r"(?<![.\w])(?:this\s*\.\s*)?transform\s*\.\s*"
        r"(?:eulerAngles|rotation|localRotation)\b",
        scan))

    types = cs2cpp._find_types(scan)
    interfaces = []
    for kind, name, _start, brace, close in types:
        if kind != "interface":
            continue
        interfaces.append({
            "name": name,
            "methods": _interface_methods(scan[brace + 1:close]),
            "path": path,
        })
    classes = []
    for kind, name, start, brace, close in types:
        if kind not in ("class", "struct"):
            continue
        # Name ends at first non-word after ``class Name`` / ``struct Name``.
        nm = re.search(
            r"(?:class|struct)\s+%s\b" % re.escape(name),
            scan[start:brace])
        name_end = start + nm.end() if nm else start
        bases = _mb_bases_from_header(scan, name_end, brace)
        body = text[brace + 1:close]
        bscan = scan[brace + 1:close]
        # Nested types are separate classes — blank their braces so outer
        # fields/methods do not absorb nested members (e.g. DragUpdater.DoUpdate
        # must not appear on _Scrollbar).
        nested = []
        for k2, n2, _s2, b2, c2 in types:
            if k2 not in ("class", "struct") or n2 == name:
                continue
            if b2 > brace and c2 < close:
                nested.append((b2 - (brace + 1), (c2 + 1) - (brace + 1)))
        body_m = _blank_index_ranges(body, nested)
        bscan_m = _blank_index_ranges(bscan, nested)
        fields = _fields_in(body_m, bscan_m, body_abs=brace + 1)
        methods = _methods_in(body_m, bscan_m, body_abs=brace + 1)
        methods.extend(
            _properties_as_methods(body_m, bscan_m, body_abs=brace + 1))
        refs = []
        for f in fields:
            if f["ty"] not in _PRIM and f["ty"] not in (
                    "Vector2", "Vector3", "Quaternion", "string"):
                refs.append(f)
        # Attributes immediately before the type declaration.
        pre = scan[max(0, start - 200):start]
        disallow_multiple = bool(re.search(
            r"\[DisallowMultipleComponent\]", pre))
        # [MaxInstances(N)]: the author's cap on live instances (see
        # `_class_index_width`). Its position, for a diagnostic.
        mi = re.search(r"(?<![\w.])MaxInstances(?:Attribute)?\s*\(\s*(\d+)"
                       r"\s*\)", pre)
        max_instances = int(mi.group(1)) if mi else None
        max_instances_at = (max(0, start - 200) + mi.start()) if mi else None
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
            "properties": _property_names(bscan_m),
            "static_getters": cs2cpp.static_getter_exprs(
                body_m, bscan_m,
                [f["name"] for f in fields] + [mm["name"] for mm in methods]
                + list(_property_names(bscan_m))),
            "static_setters": cs2cpp.static_setter_stmts(
                body_m, bscan_m,
                [f["name"] for f in fields] + [mm["name"] for mm in methods]
                + list(_property_names(bscan_m))),
            "bases": bases,
            "path": path,
            "file_text": text,
            "disallow_multiple": disallow_multiple,
            "max_instances": max_instances,
            "max_instances_at": max_instances_at,
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
        "getcomponentsinchildren_types": getcomponentsinchildren_types,
        "addcomponent_types": addcomponent_types,
        "findobject_types": findobject_types,
        "singleton_instance_types": singleton_instance_types,
        "singleton_field_types": singleton_field_types,
        "classes": classes,
        "interfaces": interfaces,
        "literals": [int(x) for x in re.findall(r"(?<![\w.])(\d+)", scan)
                     if int(x) < 1 << 20],
    }


_PRIM = ("int", "float", "bool", "byte", "short", "uint", "long",
         "double", "sbyte", "ushort", "ulong")


def _authored_string(o, name, default):
    """An instance's authored value for string field `name`: the Unity
    reader's text (`str_fields`), else a string godot_pack put in `fields`,
    else the field's initializer."""
    sf = o.get("str_fields") or {}
    if name in sf:
        return sf[name]
    v = (o.get("fields") or {}).get(name)
    return v if isinstance(v, str) else default


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


def _blank_index_ranges(s, ranges):
    """Replace [lo, hi) spans with spaces (keep newlines) for nested skip."""
    if not ranges or not s:
        return s
    chars = list(s)
    n = len(chars)
    for lo, hi in ranges:
        a = max(0, int(lo))
        b = min(n, int(hi))
        for i in range(a, b):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def _fields_in(body, bscan, body_abs=0):
    """Instance / static / const fields; methods (those with `(`) are skipped."""
    bscan = _blank_method_bodies(bscan)
    out = []
    for m in re.finditer(
            # A line's first member, or one after another's `;` on the same
            # line (method bodies are blanked, so no `for (..; ..)`).
            r"(?m)(?:^|(?<=;))[ \t]*(?:public|private|protected|internal)?"
            r"[ \t]*(?:new[ \t]+)?"
            r"[ \t]*(?:static[ \t]+)?(?:const[ \t]+)?(?:readonly[ \t]+)?"
            r"(?:event[ \t]+)?"
            # Types may be generics: Dictionary<int, int> / List<Foo>, or T[].
            r"([\w.]+(?:\s*<[^>;{\n]+>)?(?:\s*\[\s*\])?)[ \t]+(\w+)[ \t]*(=|;)",
            bscan):
        # `int F(` is a method.
        tail = body[m.end(2):m.end(2) + 16]
        if "(" in tail.split(";")[0] and "=" not in tail.split(";")[0]:
            continue
        ty, name = m.group(1).strip(), m.group(2)
        if ty in ("if", "for", "return", "new", "const", "static"):
            continue
        decl = m.group(0)
        # What Unity serializes -- and so copies on Instantiate: a public
        # field, or one marked [SerializeField] (unless [NonSerialized]).
        attrs = bscan[max(0, m.start() - 120):m.start()]
        attrs = attrs[max(attrs.rfind(";"), attrs.rfind("}"),
                          attrs.rfind("{")) + 1:]
        entry = {
            "ty": ty,
            "name": name,
            "static": bool(re.search(r"\bstatic\b", decl)),
            "const": bool(re.search(r"\bconst\b", decl)),
            "delegate": bool(re.search(r"\bevent\b", decl)) or bool(
                re.match(r"(?:System\.)?(?:Action|Func|Predicate|UnityAction)"
                         r"\b", ty)),
            "decl_abs": int(body_abs) + int(m.start()),
            "serialized": ((bool(re.match(r"\s*public\b", decl))
                            or "SerializeField" in attrs)
                           and "NonSerialized" not in attrs),
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
                if re.match(r"\s*new\s+(?:[\w.]+\.)?%s\s*\(\s*\)\s*$"
                            % re.escape(ty.split(".")[-1]), init_src):
                    entry["new_init"] = True
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


#: C# access modifiers, which `_methods_in` reads as a return type when the
#: declaration is a constructor's (`public Foo (..)`).
# Unity messages we emit. Awake runs once before Start (SettingsMenu.SetActive…).
_UNITY_EMIT_MESSAGES = frozenset({
    "Awake", "Start", "Update", "FixedUpdate", "LateUpdate",
    "OnEnable", "OnDisable", "OnDestroy",
    "OnCollisionEnter2D", "OnCollisionStay2D", "OnCollisionExit2D",
    "OnTriggerEnter2D", "OnTriggerStay2D", "OnTriggerExit2D",
    "OnJointBreak2D",
})


#: Integer types a method's parameter or result keeps at their C# width (an `int` cut a uint
#: above 2^31 and a long to 32 bits).
_WIDE_C_TYPES = {"uint": "unsigned int", "long": "long long", "ulong": "unsigned long long"}
_WIDE_ELEM_TYPES = {"uint": "uint32_t", "long": "int64_t", "ulong": "uint64_t"}


def _param_c_ty(ty):
    """C type for a C# method parameter. ``T[]`` is a pointer to T's C type."""
    ty = (ty or "").split(".")[-1].strip()
    if ty.endswith("[]") and _csharp_type_decl_kind(ty[:-2]) == "class":
        return "std::vector<int>"         # packed indices, by value
    if ty.endswith("[]"):
        elem = _param_c_ty(ty[:-2])
        return elem + ("*" if elem.endswith("*") else " *")
    if ty in ("float", "double"):
        return "float"
    if ty == "string":
        return "const char *"
    if ty in _WIDE_C_TYPES:
        return _WIDE_C_TYPES[ty]
    if ty in ("byte", "sbyte", "short", "ushort", "int", "bool", "char"):
        return "int"
    if ty in ("Vector2", "Rect"):
        # The engine's value struct -- it fell to `int` below, a handle.
        return ty
    # MonoBehaviour / component / enum handles → packed index.
    return "int"


#: Project root whose C# sources say what kind of type a name is, and the
#: per-root name → {kinds} index built from them on first use.
_TYPE_DECL_ROOT = [None]
_TYPE_DECL_KINDS = {}


def _csharp_type_decl_kind(name):
    """``enum`` / ``struct`` / ``class`` / ``interface`` for a type the
    project or its packages declare, or None when it is undeclared or
    declared as more than one kind (then nothing can be assumed)."""
    root = _TYPE_DECL_ROOT[0]
    if not root:
        return None
    kinds = _TYPE_DECL_KINDS.get(root)
    if kinds is None:
        kinds = {}
        pat = re.compile(r"\b(enum|struct|class|interface)\s+([A-Za-z_]\w*)")
        for sub in ("Assets", "Packages", os.path.join("Library", "PackageCache")):
            for dp, _dn, fns in os.walk(os.path.join(root, sub)):
                for fn in fns:
                    if not fn.endswith(".cs"):
                        continue
                    try:
                        with open(os.path.join(dp, fn), encoding="utf-8",
                                  errors="replace") as f:
                            text = f.read()
                    except OSError:
                        continue
                    for m in pat.finditer(cs2cpp._blank(text)):
                        kinds.setdefault(m.group(2), set()).add(m.group(1))
        _TYPE_DECL_KINDS[root] = kinds
    found = kinds.get(name) or set()
    return next(iter(found)) if len(found) == 1 else None


#: UnityEngine enums: an integer in the packed engine, `default(T)` is 0.
_UNITY_ENUM_TYPES = frozenset((
    "KeyCode", "ForceMode2D", "RigidbodyType2D", "Space", "TouchPhase",
    "RigidbodyInterpolation2D", "CollisionDetectionMode2D", "SendMessageOptions",
    "PrimitiveType", "FilterMode", "WrapMode", "HideFlags", "LogType",
    "RuntimePlatform", "FullScreenMode", "CursorLockMode", "LoadSceneMode",
))


def _default_arg_c(expr, ty=None):
    """C for a C# default parameter value, or None when it needs type-aware
    lowering this does not do (enum members, constants, expressions).
    ``null`` / ``default`` need the parameter type *ty*: a packed handle is
    -1 (the packed model's null), a string or array pointer is 0, a number
    or enum is 0; ``default(T)`` names its type. Structs and nullable value
    types (``int?``) are not modelled."""
    e = (expr or "").strip()
    dm = re.fullmatch(r"default\s*\(\s*([\w.]+)\s*\)", e)
    if dm:
        e, ty = "default", dm.group(1)
    if re.fullmatch(r"-?\d+[uUlL]*", e):
        return re.sub(r"[uUlL]+$", "", e)
    if re.fullmatch(r"-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?[fFdDmM]?", e):
        return re.sub(r"[dDmM]$", "", e)
    if e == "true":
        return "1"
    if e == "false":
        return "0"
    fl = {"float.NegativeInfinity": "(-INFINITY)",
          "float.PositiveInfinity": "INFINITY",
          "float.MaxValue": "3.40282347e+38f",
          "float.MinValue": "(-3.40282347e+38f)"}.get(re.sub(r"\s+", "", e))
    if fl:
        return fl
    if re.fullmatch(r'"(?:[^"\\\n]|\\.)*"', e):
        return e
    if e in ("null", "default") and ty and not ty.endswith("?"):
        cty = _param_c_ty(ty)
        if cty.endswith("*"):
            return "0"
        base = ty.split(".")[-1]
        if e == "null" and base not in _PRIMITIVE_PARAM_TYPES:
            return "-1"
        if e == "default":
            if base in _PRIMITIVE_PARAM_TYPES or base in _UNITY_ENUM_TYPES:
                return "0"
            if base == "Vector2":
                return "Vector2_make(0.f, 0.f)"
            kind = _csharp_type_decl_kind(base)
            if kind == "enum":
                return "0"
            if kind in ("class", "interface"):
                return "-1"
    return None


# Numeric C# types a class-level static field is emitted for (as int / float).
_STATIC_NUMERIC_TYPES = frozenset((
    "float", "double", "byte", "sbyte", "short", "ushort", "int", "uint",
    "long", "ulong"))

# C# value types that cannot be null (their `default` is 0).
_PRIMITIVE_PARAM_TYPES = frozenset((
    "float", "double", "byte", "sbyte", "short", "ushort", "int", "uint",
    "long", "ulong", "bool", "char", "decimal"))

# `name: value` -- a named argument, not `a ? b : c` or `global::X`.
_NAMED_ARG_RE = re.compile(r"\s*([A-Za-z_]\w*)\s*:(?!:)\s*(.*\S)\s*$", re.S)


def _call_with_defaults(args, params):
    """Argument text *args* of a call, completed against *params*: named
    arguments (``by: 2``) are put in declaration order and missing trailing
    parameters get their defaults. Unchanged when nothing is missing or
    named, or when an argument cannot be placed or a default cannot be
    lowered (the pack then refuses the call, as before)."""
    given = cs2cpp.split_call_args(args) if args.strip() else []
    positional = []
    named = {}
    for g in given:
        m = _NAMED_ARG_RE.match(g)
        if m:
            named[m.group(1)] = m.group(2)
        elif named:
            return args  # positional after named: not C# we lower
        else:
            positional.append(g)
    if not named and len(positional) >= len(params):
        return args
    if len(positional) > len(params):
        return args
    names = [prm.name for prm in params]
    if any(n not in names or names.index(n) < len(positional) for n in named):
        return args
    out = list(positional)
    for prm in params[len(positional):]:
        if prm.name in named:
            out.append(named[prm.name])
            continue
        c = (_default_arg_c(prm.default, prm.type)
             if prm.default is not None else None)
        if c is None:
            return args
        out.append(c)
    if not named:
        return ", ".join(([args.strip()] if positional else []) + out[len(positional):])
    return ", ".join(out)


def _fill_defaults_after(text, sym, params, receiver=False):
    """Fill missing default arguments in every ``sym(...)`` call in *text*
    (*receiver*: an instance method's, whose first argument is the
    receiver's index)."""
    if not any(prm.default is not None for prm in params):
        return text
    out = []
    pos = 0
    pat = re.compile(r"(?<![\w.])%s\s*\(" % re.escape(sym))
    while True:
        m = pat.search(text, pos)
        if not m:
            break
        open_i = m.end() - 1
        args, end = _match_call_args(text, open_i)
        if end <= open_i:
            break
        if receiver:
            parts = cs2cpp.split_call_args(args) if args.strip() else []
            new_args = args if not parts else ", ".join(
                [parts[0]] + ([_call_with_defaults(", ".join(parts[1:]),
                                                   params)]
                              if len(parts) > 1 or params else []))
            new_args = cs2cpp.lower_float_literals(new_args.rstrip(", "))
        else:
            new_args = _call_with_defaults(args, params)
        out.append(text[pos:open_i + 1])
        out.append(new_args)
        out.append(")")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _ret_c_ty(ret, plan):
    """C return type for a C# method's, or None when the packed engine has
    no value for it (a coroutine's IEnumerator, a Vector2, a collection):
    such a method stays a stub. "void" for void."""
    t = (ret or "void").strip()
    base = t.split(".")[-1]
    if base == "void":
        return "void"
    if base in ("float", "double"):
        return "float"
    if base == "string":
        return "const char *"
    if base in _WIDE_C_TYPES:
        return _WIDE_C_TYPES[base]
    if base in ("byte", "sbyte", "short", "ushort", "int", "bool", "char"):
        return "int"
    if base in (plan.get("classes") or {}) or base in (
            "GameObject", "Transform"):
        return "int"                      # a packed index / GameObject
    if base in ("Vector2", "Rect"):
        return base                       # the engine's value struct
    if base.endswith("[]") and base[:-2] in (plan.get("classes") or {}):
        return "std::vector<int>"         # packed indices, copied out
    return None


def _method_ret_kinds(plan):
    """{C symbol: "s" / "i" / "b" / "f"} for every method that returns a
    value, so a call is typed where it is used (`"x" + Score()`)."""
    got = plan.get("_method_ret_kinds_cache")
    if got is not None:
        return got
    got = {}
    for cname, pairs in (plan.get("_methods_by") or {}).items():
        idn = _c_ident(cname)
        for _c, m in pairs:
            base = (m.get("ret") or "void").strip().split(".")[-1]
            if _ret_c_ty(base, plan) in (None, "void", "Rect"):
                continue
            kind = ("s" if base == "string" else "b" if base == "bool"
                    else "c" if base == "char"
                    else "f" if base in ("float", "double")
                    else "v" if base == "Vector2" else "i")
            for ov in (False, True):
                got[_method_c_symbol(idn, m["name"], m.get("args") or "",
                                     ov)] = kind
    plan["_method_ret_kinds_cache"] = got
    return got


def _method_c_params(args_str):
    """C param list string from C# ``(byte amount, Cosmetic c)``."""
    return ", ".join("%s %s" % (_param_c_ty(p.type), p.name)
                     for p in cs2cpp.parse_params(args_str))


_ARG_INT_TYPES = frozenset(("int", "uint", "long", "ulong", "short",
                            "ushort", "byte", "sbyte"))


def _overload_arg_fit(ptype, kind):
    """How well an argument of scalar *kind* (``s``/``c``/``i``/``f``) fits a
    parameter of C# type *ptype*: 0 no, 1 convertible, 2 exact."""
    t = (ptype or "").split(".")[-1]
    if t == "string":
        return 2 if kind == "s" else 0
    if kind == "s":
        return 0
    if t in _ARG_INT_TYPES:
        return 2 if kind == "i" else 1
    if t in ("float", "double"):
        return 2 if kind == "f" else 1
    if t == "char":
        return 2 if kind == "c" else 1
    return 1


def _pick_overload(cands, args, string_idents):
    """The one overload in *cands* that the (lowered) argument text *args*
    selects, or None when none or several fit equally well."""
    given = cs2cpp.split_call_args(args) if args.strip() else []
    if any(_NAMED_ARG_RE.match(g) for g in given):
        return None
    best, best_score, tie = None, -1, False
    for m in cands:
        params = cs2cpp.parse_params(m.get("args") or "")
        need = sum(1 for prm in params if prm.default is None)
        if not need <= len(given) <= len(params):
            continue
        score = 0
        for g, prm in zip(given, params):
            fit = _overload_arg_fit(
                prm.type, _c_expr_scalar_kind(g, string_idents=string_idents))
            if not fit:
                break
            score += fit
        else:
            if score > best_score:
                best, best_score, tie = m, score, False
            elif score == best_score:
                tie = True
    return None if tie else best


def _rewrite_method_calls(text, call_re, cands, oidn, receiver, overloaded,
                          string_idents, protos=None):
    """Calls matched by *call_re* (through the opening parenthesis) to one of
    *cands* (same-named methods of class *oidn*) → ``Sym(receiver, args)``.
    Overloads are picked by argument type; a call none or several fit is
    left as written. *protos* collects the callee prototypes."""
    pat = re.compile(call_re)
    out = []
    pos = 0
    while True:
        m = pat.search(text, pos)
        if not m:
            break
        open_i = m.end() - 1
        args, end = _match_call_args(text, open_i)
        if end <= open_i:
            break
        args = _rewrite_method_calls(args, call_re, cands, oidn, receiver,
                                     overloaded, string_idents, protos)
        meth = (_pick_overload(cands, args, string_idents) if overloaded
                else cands[0])
        out.append(text[pos:m.start()])
        if meth is None:
            out.append(text[m.start():open_i + 1] + args + ")")
            pos = end
            continue
        margs = meth.get("args") or ""
        sym = _method_c_symbol(oidn, meth["name"], margs, overloaded)
        a = _call_with_defaults(args, cs2cpp.parse_params(margs)).strip()
        a = _gather_params_args(a, margs, text)
        out.append("%s(%s%s)" % (sym, receiver, (", " + a) if a else ""))
        if protos is not None:
            plist = _method_c_params(margs)
            protos.add("static void %s(unsigned i%s);"
                       % (sym, (", " + plist) if plist else ""))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _sub_calls(text, name, fn):
    """Each `name(args)` call (not a member's), its parentheses matched,
    as `fn(args)`; calls nested in the arguments first."""
    pat = re.compile(r"(?<![\w.])%s\s*\(" % re.escape(name))
    out, pos = [], 0
    scan = cs2cpp._blank(text)
    for m in pat.finditer(scan):
        if m.start() < pos:
            continue
        got = _match_call_args(scan, m.end() - 1)
        if not got:
            continue
        end = got[1]
        args = text[m.end():end - 1]
        out.append(text[pos:m.start()] + fn(_sub_calls(args, name, fn)))
        pos = end
    return "".join(out) + text[pos:]


def _gather_params_args(args, margs, text):
    """A `params Cls[] xs` method's trailing arguments, gathered into the
    vector its C parameter is. One argument named as an array or vector in
    *text* is passed whole, as C# does."""
    params = cs2cpp.parse_params(margs)
    if not params or not re.search(r"\bparams\s", margs) \
            or _param_c_ty(params[-1].type) != "std::vector<int>":
        return args
    given = cs2cpp.split_call_args(args) if args.strip() else []
    fixed, rest = given[:len(params) - 1], given[len(params) - 1:]
    # ponytail: "an array" is only a name declared `T[]` / std::vector<int>
    # in the body; any other single array-valued expression is wrapped
    if len(rest) == 1 and re.search(
            r"(?:\[\s*\]|std::vector<int>\s*&?)\s*%s\s*[,);=]"
            % re.escape(rest[0].strip()), cs2cpp._blank(text)):
        return args
    v = "_cs_vec_new_int()"
    for r in rest:
        v = "_cs_vec_push_int(%s, %s)" % (v, r.strip())
    return ", ".join([g.strip() for g in fixed] + [v])


def _returns_void(m):
    """Methods emit as `static void`; a valued one is always a stub."""
    return (m.get("ret") or "void").strip().split(".")[-1] == "void"


def _rewrite_singleton_method_calls(text, plan, cl, site, string_idents):
    """``Other.Instance.Method(args)`` (already ``Other_Instance().Method(``)
    → ``Other_Method(<Other_Instance() or NullReferenceException>, args)``
    for the instance methods ``Other`` emits."""
    if "_Instance()" not in text:
        return text
    nre_site = site or {"class": cl.get("name") or "?", "method": "?",
                        "path": "?"}
    for ocname, pairs in (plan.get("_methods_by") or {}).items():
        ocl = (plan.get("classes") or {}).get(ocname) or {}
        if ocl.get("ctor_forbidden"):
            continue
        oidn = _c_ident(ocname)
        inst_re = r"(?<![\w.])%s_Instance\(\)\s*\.\s*" % re.escape(oidn)
        if not re.search(inst_re, text):
            continue
        emit_names = _reachable_emit_methods(
            [m for _c, m in pairs],
            _extra_roots(plan, ocname))
        overloaded = _overload_method_names(
            [m for _c, m in pairs
             if m["name"] in emit_names and m["name"] != "OnEnable"])
        groups = {}
        for _c, m in pairs:
            n = m.get("name") or ""
            if (m.get("static") or n not in emit_names or n == "OnEnable"
                    or n in _UNITY_EMIT_MESSAGES or n in _COLLISION2D_MSGS
                    or not _returns_void(m)):
                continue
            groups.setdefault(n, []).append(m)
        receiver = (
            "({ int _up_recv = %s_Instance(); if (_up_recv < 0) %s; "
            "(unsigned)_up_recv; })" % (oidn, _nre_at_expr(nre_site, 0)))
        protos = site.setdefault("protos", set()) if site is not None else None
        for n in sorted(groups, key=len, reverse=True):
            text = _rewrite_method_calls(
                text, inst_re + re.escape(n) + r"\s*\(", groups[n], oidn,
                receiver, n in overloaded, string_idents, protos)
    return text


#: `T[]` elements that are values, not references to objects.
_VALUE_ELEM_TYPES = frozenset((
    "string", "bool", "byte", "sbyte", "char", "short", "ushort", "int",
    "uint", "long", "ulong", "float", "double", "Vector2", "Vector3",
    "Vector2Int", "Vector3Int", "Color", "Color32", "Rect", "Quaternion"))


_NUMBER_ARRAY_ELEMS = frozenset(("float", "double", "int", "bool"))


def _array_elem_name(ty):
    """Element type from ``T[]``, or None."""
    if not ty:
        return None
    m = re.match(r"^([\w.]+)\s*\[\s*\]\s*$", str(ty).strip())
    return m.group(1).split(".")[-1] if m else None


def _collect_static_getters(analyses):
    """``{class: {property: expr}}`` from ``cs2cpp.static_getter_exprs``."""
    out = {}
    for a in analyses:
        for c in a.get("classes") or []:
            if c.get("static_getters"):
                out.setdefault(c["name"], {}).update(c["static_getters"])
    return out


def _collect_static_setters(analyses):
    """``{class: {property: stmt}}`` from ``cs2cpp.static_setter_stmts``."""
    out = {}
    for a in analyses:
        for c in a.get("classes") or []:
            if c.get("static_setters"):
                out.setdefault(c["name"], {}).update(c["static_setters"])
    return out


_STATIC_HELPERS = {}


def _static_helper_methods():
    """``{class: {method: (params, text, is_void)}}`` for the one-statement
    static methods (``cs2cpp.static_method_exprs``) of the project's
    ``static class``es -- helpers no scene names, so they are not analyzed."""
    import tools.cpprust as cpprust
    root = _TYPE_DECL_ROOT[0]
    if not root:
        return {}
    got = _STATIC_HELPERS.get(root)
    if got is not None:
        return got
    got, seen = {}, {}
    head = re.compile(r"\bstatic\s+(?:partial\s+)?class\s+([A-Za-z_]\w*)[^{;]*\{")
    field = re.compile(
        r"(?m)^\s*(?:(?:public|private|protected|internal|static|readonly|"
        r"const)\s+)+[\w.<>\[\],]+\s+(\w+)\s*[;=]")
    for dp, _dn, fns in os.walk(os.path.join(root, "Assets")):
        for fn in sorted(fns):
            if not fn.endswith(".cs"):
                continue
            try:
                # Through `_read`: the source overlay's text, extension
                # methods already plain static ones.
                text = _read(os.path.join(dp, fn))
            except (OSError, UnicodeDecodeError):
                continue
            if "static" not in text:
                continue
            bscan = cs2cpp._blank(text)
            for m in head.finditer(bscan):
                open_i = m.end() - 1
                close = cpprust._match_brace(bscan, open_i)
                if close is None:
                    continue
                body, bs = text[open_i + 1:close], bscan[open_i + 1:close]
                cname = m.group(1)
                seen[cname] = seen.get(cname, 0) + 1
                # ponytail: C has only Vector2, so `SetX(Vector2)` stands in
                # for its Vector3 overloads; a Vector3Int receiver is not told apart
                meths = cs2cpp.static_method_exprs(body, bs, field.findall(bs),
                                                   prefer_first="Vector2")
                if meths:
                    got.setdefault(cname, {}).update(meths)
    for cname, n in seen.items():
        if n > 1:
            got.pop(cname, None)
    _STATIC_HELPERS[root] = got
    return got


_SIMPLE_ARG_RE = re.compile(
    r'[A-Za-z_][\w.]*|"(?:[^"\\]|\\.)*"|-?(?:\d+\.?\d*|\.\d+)[fF]?'
    # a packed accessor of a name (no side effect, so read twice is the same)
    r'|[A-Za-z_]\w*_get_\w+\(\s*\w*\s*\)(?:\s*\.\s*\w+)*')


def _bind_params(text, params_str, args):
    """*text* with each parameter of *params_str* replaced by its argument
    from *args* (or its default), parenthesized; None when an argument is
    missing or named, or a non-trivial argument would be evaluated twice."""
    prms = cs2cpp.parse_params(params_str)
    given = cs2cpp.split_call_args(args) if args.strip() else []
    if len(given) > len(prms) or any(_NAMED_ARG_RE.match(g) for g in given):
        return None
    vals = []
    for k, prm in enumerate(prms):
        if k < len(given):
            vals.append(given[k].strip())
        elif prm.default is not None:
            vals.append(prm.default.strip())
        else:
            return None
    blank = cs2cpp._blank(text)
    for prm, v in zip(prms, vals):
        uses = len(re.findall(r"(?<![\w.])%s\b" % re.escape(prm.name), blank))
        if uses > 1 and not _SIMPLE_ARG_RE.fullmatch(v):
            return None
    if not prms:
        return text
    for prm in prms:
        hash_of = {"bool": "(%s ? 1 : 0)" % prm.name,
                   "int": "(%s)" % prm.name}.get(prm.type)
        if hash_of:
            text = cs2cpp.code_sub(
                r"(?<![\w.])%s\s*\.\s*GetHashCode\s*\(\s*\)" % re.escape(prm.name),
                hash_of, text)
    # A plain name (or literal) is bound as itself: `go.activeInHierarchy`
    # with `go` bound to `gameObject` is `gameObject.activeInHierarchy`,
    # which the member lowering reads -- `(gameObject).activeInHierarchy`
    # it did not.
    names = {p.name: (v.strip() if re.match(
                          r'^(?:[A-Za-z_]\w*|"(?:[^"\\]|\\.)*"|\d+(?:\.\d+)?[fF]?)$',
                          v.strip())
                      else "(%s)" % v)
             for p, v in zip(prms, vals)}
    return cs2cpp.code_sub(
        r"(?<![\w.])(%s)\b" % "|".join(re.escape(n) for n in names),
        lambda m: names[m.group(1)], text)


def _inline_static_helpers(text, plan):
    """Calls of a one-statement static helper (``_static_helper_methods``)
    become its body with the arguments bound: an expression in parentheses,
    or -- for a ``void`` helper called as a statement -- the statement."""
    classes = plan.get("classes") or {}
    for cname, meths in sorted(_static_helper_methods().items()):
        if cname in classes or cname not in text:
            continue
        for mname, (params, body, is_void) in sorted(meths.items()):
            pat = re.compile(r"(?<![\w.])(?:\w+\s*\.\s*)*%s\s*\.\s*%s\s*\("
                             % (re.escape(cname), re.escape(mname)))
            out, pos = [], 0
            while True:
                m = pat.search(text, pos)
                if not m:
                    break
                open_i = m.end() - 1
                args, end = _match_call_args(text, open_i)
                if end <= open_i:
                    break
                bound = _bind_params(body, params, args)
                if bound is not None and is_void and not re.match(
                        r"\s*;", text[end:]):
                    bound = None
                out.append(text[pos:m.start()])
                if bound is None:
                    out.append(text[m.start():end])
                else:
                    out.append(bound if is_void else "(%s)" % bound)
                pos = end
            out.append(text[pos:])
            text = "".join(out)
    return text


def _inline_static_setters(text, cl, plan):
    """``Type.Name = x;`` (or bare ``Name = x;`` inside *Type*) for an
    inlinable static setter becomes the setter's statement on ``x``."""
    for cname, props in (plan.get("static_setters") or {}).items():
        for pname, stmt in props.items():
            pats = [r"(?:[\w.]+\.)?%s\s*\.\s*%s" % (re.escape(cname),
                                                   re.escape(pname))]
            if cname == cl.get("name"):
                pats.append(re.escape(pname))
            for pat in pats:
                def rep(m, stmt=stmt):
                    bound = _bind_params(stmt, "object value", m.group(1))
                    return m.group(0) if bound is None else bound + ";"
                text = cs2cpp.code_sub(
                    r"(?<![\w.])%s\s*=(?!=)\s*([^;]+);" % pat, rep, text)
    return text


_PLAYER_PREFS_DEFAULTS = {"GetString": '""', "GetInt": "0", "GetFloat": "0.f"}


def _lower_player_prefs(text):
    """UnityEngine.PlayerPrefs → the engine's persistent key/value store;
    a missing default argument is Unity's ("" / 0 / 0f)."""
    pat = re.compile(r"(?<![\w.])(?:UnityEngine\s*\.\s*)?PlayerPrefs\s*\.\s*"
                     r"(GetString|GetInt|GetFloat|SetString|SetInt|SetFloat|"
                     r"HasKey|DeleteKey|DeleteAll|Save)\s*\(")
    out, pos = [], 0
    while True:
        m = pat.search(text, pos)
        if not m:
            break
        open_i = m.end() - 1
        args, end = _match_call_args(text, open_i)
        if end <= open_i:
            break
        args = _lower_player_prefs(args)
        fn = m.group(1)
        if fn in _PLAYER_PREFS_DEFAULTS and len(
                cs2cpp.split_call_args(args) if args.strip() else []) == 1:
            args = "%s, %s" % (args.strip(), _PLAYER_PREFS_DEFAULTS[fn])
        out.append(text[pos:m.start()])
        out.append("PlayerPrefs_%s(%s)" % (fn, args.strip()))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _uses_player_prefs(analyses):
    """Whether a packed script -- or a static helper one names -- touches
    PlayerPrefs."""
    helpers = {c for c, ms in _static_helper_methods().items()
               if any("PlayerPrefs" in t for _p, t, _v in ms.values())}
    for a in analyses:
        for c in a.get("classes") or []:
            text = c.get("file_text") or ""
            if re.search(r"(?<![\w.])PlayerPrefs\s*\.", text):
                return True
            if any(re.search(r"(?<![\w.])%s\s*\." % re.escape(h), text)
                   for h in helpers):
                return True
    return False


def _inline_static_getters(text, cl, plan):
    """Reads of an inlinable static property (``Type.Name``, or bare ``Name``
    inside *Type*) become ``(expr)``. Assignments, ``++`` and ``--`` are
    left alone."""
    getters = plan.get("static_getters") or {}
    for cname, props in getters.items():
        for pname, expr in props.items():
            names = [r"(?:[\w.]+\.)?%s\s*\.\s*%s" % (re.escape(cname), re.escape(pname))]
            if cname == cl.get("name"):
                names.append(re.escape(pname))
            for pat in names:
                text = cs2cpp.code_sub(
                    r"(?<!\+\+)(?<!--)(?<![\w.])%s\b"
                    r"(?!\s*(?:(?:[-+*/%%&|^]|<<|>>|\?\?)?=(?!=)|\+\+|--))" % pat,
                    "(%s)" % cs2cpp.lower_float_literals(expr), text)
    return text


_SCENE_MANAGER = r"(?<![\w.])(?:UnityEngine\s*\.\s*SceneManagement\s*\.\s*)?SceneManager\s*\.\s*"


def _lower_scene_manager(text, string_idents=(), scene_idents=()):
    """UnityEngine.SceneManagement → the packed SceneManager (a Scene is its
    build index; loads apply at the start of the next frame).

    ``LoadScene`` / ``LoadSceneAsync`` / ``UnloadSceneAsync`` take a build
    index, a scene name or path, or a Scene; ``GetActiveScene``,
    ``GetSceneByBuildIndex`` / ``GetSceneByName``, ``sceneCount`` and
    ``sceneCountInBuildSettings`` read the scene tables; a Scene's ``name``,
    ``path``, ``buildIndex`` and ``isLoaded`` do too."""
    text = cs2cpp.code_sub(
        r"(?<![\w.])(?:UnityEngine\s*\.\s*SceneManagement\s*\.\s*)?"
        r"LoadSceneMode\s*\.\s*(Single|Additive)\b",
        lambda m: "1" if m.group(1) == "Additive" else "0", text)
    strings = set(string_idents)

    def _is_string(arg):
        a = arg.strip()
        while a.startswith("(") and a.endswith(")"):
            a = a[1:-1].strip()
        return (a.startswith('"') or a.startswith("$\"")
                or re.fullmatch(r"\w+", a) is not None and a in strings
                or re.search(r"\.\s*name\s*$|Scene_name\s*\(|\.\s*ToString\s*\(", a)
                is not None
                or re.search(r'"\s*\+|\+\s*"', a) is not None
                or _c_expr_scalar_kind(a) == "s")

    def _calls(t, name_re, emit):
        out, pos = [], 0
        pat = re.compile(_SCENE_MANAGER + name_re + r"\s*\(")
        while True:
            m = pat.search(t, pos)
            if not m:
                break
            args, end = _match_call_args(t, m.end() - 1)
            if end <= m.end() - 1:
                break
            parts = cs2cpp.split_call_args(args) if args.strip() else []
            out.append(t[pos:m.start()])
            out.append(emit(m, parts))
            pos = end
        out.append(t[pos:])
        return "".join(out)

    def _load(m, parts):
        if not parts:
            return m.group(0) + ")"
        mode = parts[1].strip() if len(parts) > 1 else "0"
        fn = ("SceneManager_LoadSceneName" if _is_string(parts[0])
              else "SceneManager_LoadScene")
        return "%s(%s, %s)" % (fn, parts[0].strip(), mode)

    def _unload(m, parts):
        if len(parts) != 1:
            return m.group(0) + ", ".join(parts) + ")"
        fn = ("SceneManager_UnloadSceneName" if _is_string(parts[0])
              else "SceneManager_UnloadScene")
        return "%s(%s)" % (fn, parts[0].strip())

    text = _calls(text, r"(?:LoadSceneAsync|LoadScene)", _load)
    text = _calls(text, r"(?:UnloadSceneAsync|UnloadScene)", _unload)
    text = cs2cpp.code_sub(_SCENE_MANAGER + r"GetActiveScene\s*\(\s*\)",
                           "(_engine_scene_active)", text)
    text = cs2cpp.code_sub(_SCENE_MANAGER + r"GetSceneByBuildIndex\s*\(",
                           "_engine_scene_by_index(", text)
    text = cs2cpp.code_sub(_SCENE_MANAGER + r"GetSceneByName\s*\(",
                           "_engine_scene_index_of(", text)
    text = cs2cpp.code_sub(_SCENE_MANAGER + r"sceneCountInBuildSettings\b",
                           "_engine_scene_count", text)
    text = cs2cpp.code_sub(_SCENE_MANAGER + r"sceneCount\b",
                           "SceneManager_sceneCount()", text)
    # Scene members: on a Scene expression in parentheses, or a Scene local.
    members = {"name": "Scene_name(%s)", "path": "Scene_path(%s)",
               "isLoaded": "Scene_isLoaded(%s)", "buildIndex": "(%s)"}
    for _pass in range(4):
        text = cs2cpp.code_sub(
            r"\(\s*\(\s*_engine_scene_active\s*\)\s*\)",
            "(_engine_scene_active)", text)
    text = cs2cpp.code_sub(
        r"(\(_engine_scene_active\)"
        r"|_engine_scene_(?:index_of|by_index)\([^()]*\))"
        r"\s*\.\s*(name|path|isLoaded|buildIndex)\b",
        lambda m: members[m.group(2)] % m.group(1), text)
    for ident in scene_idents:
        text = cs2cpp.code_sub(
            r"(?<![\w.])%s\s*\.\s*(name|path|isLoaded|buildIndex)\b" % re.escape(ident),
            lambda m, ident=ident: members[m.group(1)] % ident, text)
    return text


def _collect_interfaces(analyses):
    """Authored interface name → what it declares (`IUpdatable.DoUpdate`)."""
    out = {}
    for a in analyses or []:
        for i in a.get("interfaces") or []:
            out[i["name"]] = i
    return out


#: The tagged reference an interface-typed collection holds. A `T[]` of a
#: packed class is a vector of indices into that one array; an `IUpdatable[]`
#: names instances of *different* classes, so each element carries which.
_IREF_TYPE = "_engine_iref"

#: An interface-typed local or parameter is one int: the implementor's class
#: id above _IREF_BITS, its instance below; -1 is null.
# ponytail: 2^20 instances a class, 2^11 classes; a wider int if one outgrows it
_IREF_BITS = 20


def _iface_holds(text, plan, site):
    """{name: interface} of a body's interface-typed parameters and locals."""
    ifaces = plan.get("interfaces") or {}
    holds = {prm.name: prm.type.split(".")[-1]
             for prm in cs2cpp.parse_params((site or {}).get("args") or "")
             if prm.type.split(".")[-1] in ifaces}
    for m in re.finditer(r"(?<![\w.])([A-Za-z_][\w.]*)\s+([A-Za-z_]\w*)\s*[=;]",
                         cs2cpp._blank(text)):
        if m.group(1).split(".")[-1] in ifaces:
            holds[m.group(2)] = m.group(1).split(".")[-1]
    return holds


def _iface_dispatch_sym(iface, mname):
    return "%s_%s" % (_c_ident(iface), mname)


def _iface_method_calls(text, plan, site):
    """`d.M(a)` on an interface-typed name is the interface's dispatcher,
    `IFace_M(d, a)` (`_emit_iface_dispatch`); the names are kept for
    `_null_handle_args`, which lets only them reach an interface slot."""
    holds = _iface_holds(text, plan, site)
    plan["_iface_names_cur"] = set(holds)
    ifaces = plan.get("interfaces") or {}
    for recv, iface in sorted(holds.items()):
        for meth in ifaces[iface].get("methods") or []:
            sym = _iface_dispatch_sym(iface, meth["name"])
            pat = r"(?<![\w.])%s\s*\.\s*%s\s*\(\s*(\)?)" % (
                re.escape(recv), re.escape(meth["name"]))
            if not re.search(pat, cs2cpp._blank(text)):
                continue
            plan.setdefault("_iface_used", set()).add((iface, meth["name"]))
            if site is not None:
                site.setdefault("protos", set()).add(
                    _iface_dispatch_proto(iface, meth, plan) + ";")
            text = cs2cpp.code_sub(
                pat, lambda m, s=sym, r=recv: "%s(%s%s" % (
                    s, r, ")" if m.group(1) else ", "), text)
    return text


def _iface_dispatch_proto(iface, meth, plan):
    ret = _ret_c_ty((meth.get("ret") or "void").split(".")[-1], plan) or "void"
    prm = _method_c_params(meth.get("args") or "")
    return "static %s %s(int r%s)" % (
        ret, _iface_dispatch_sym(iface, meth["name"]), ", " + prm if prm else "")


def _emit_iface_dispatch(p, plan):
    """Each interface method a body calls: a switch on the reference's class
    to that implementor's packed method; a null reference raises."""
    by = plan.get("_methods_by") or {}
    ifaces = plan.get("interfaces") or {}
    for iface, mname in sorted(plan.get("_iface_used") or ()):
        meth = next(m for m in ifaces[iface]["methods"] if m["name"] == mname)
        ret = _ret_c_ty((meth.get("ret") or "void").split(".")[-1], plan) or "void"
        names = [prm.name for prm in cs2cpp.parse_params(meth.get("args") or "")]
        p(_iface_dispatch_proto(iface, meth, plan) + " {")
        p("    /* interface dispatch */")
        p("    switch (r < 0 ? -1 : r >> %d) {" % _IREF_BITS)
        for cname in _iface_implementors(iface, plan):
            ms = [m for _c, m in by.get(cname, [])]
            over = _overload_method_names(ms)
            sym = (_method_c_symbol(_c_ident(cname), mname, meth.get("args") or "", True)
                   if mname in over else "%s_%s" % (_c_ident(cname), mname))
            call = "%s((unsigned)(r & %du)%s)" % (
                sym, (1 << _IREF_BITS) - 1, "".join(", " + n for n in names))
            p("    case %d: %s;" % (_class_id(plan, cname), (
                "return " + call) if ret != "void" else call + "; return"))
        p("    }")
        p("    (void)_engine_nre_ix(4294967295u);")
        if ret != "void":
            p("    return (%s)0;" % ret)
        p("}")
        p("")


def _emit_iface_get_component(p, plan, used_apis, analyses):
    """GetComponent<IFace> / GetComponentInParent<IFace>: the first
    implementor on the GameObject (or up its parents), as an interface ref.
    ponytail: implementors tried in class-name order, not component order"""
    ifaces = plan.get("interfaces") or {}
    want = {a.split("<", 1)[1][:-1] for a in used_apis
            if a.startswith("GetComponentInParent<")} & set(ifaces)
    for iface in sorted(want | ({t for a in analyses or ()
                                 for t in a.get("getcomponent_types") or ()}
                                & set(ifaces))):
        idn = _c_ident(iface)
        p("static int GameObject_GetComponent_%s(int go) {" % idn)
        p("    int ci;")
        for cname in _iface_implementors(iface, plan):
            p("    ci = GameObject_GetComponent_%s(go);" % _c_ident(cname))
            p("    if (ci >= 0) return (%d << %d) | ci;" % (
                _class_id(plan, cname), _IREF_BITS))
        p("    return -1;")
        p("}")
        if iface in want:
            p("static int GameObject_GetComponentInParent_%s(int go) {" % idn)
            p("    int r, guard;")
            p("    for (guard = 0; go >= 0 && guard < _engine_go_count + 2;"
              " guard = guard + 1) {")
            p("        r = GameObject_GetComponent_%s(go);" % idn)
            p("        if (r >= 0) return r;")
            p("        go = Transform_get_parent(go);")
            p("    }")
            p("    return -1;")
            p("}")
        p("")


def _static_ref_array(f, plan):
    """A static ``T[]`` the pack holds as a vector: (elem, interface?) or None.

    `T` a packed class is a vector of its instance indices; `T` an authored
    interface is a vector of `_engine_iref`, since its implementors live in
    different instance arrays and an index alone would not say which.
    """
    if not (f.get("static") or f.get("const")):
        return None
    elem = _array_elem_name(f.get("ty") or "")
    if not elem:
        return None
    if elem in (plan.get("interfaces") or {}):
        return elem, True
    if elem in (plan.get("classes") or {}):
        return elem, False
    return None


def _static_ref_arrays(cl, plan):
    """`cl`'s static ref arrays, as (field, element, interface?)."""
    out = []
    for f in cl.get("class_consts") or []:
        got = _static_ref_array(f, plan)
        if got:
            out.append((f["name"], got[0], got[1]))
    return out


def _any_static_ref_arrays(plan):
    """Every static ref array in the plan, as (class, field, elem, iface?)."""
    out = []
    for cname, cl in sorted((plan.get("classes") or {}).items()):
        for fname, elem, iface in _static_ref_arrays(cl, plan):
            out.append((cname, fname, elem, iface))
    return out


def _iface_implementors(iface, plan):
    """Packed classes whose bases name *iface* (`class _Scrollbar : IUpdatable`)."""
    bases = plan.get("mb_bases") or {}
    return [c for c in sorted(plan.get("classes") or ())
            if _mb_is_a(c, iface, bases)]


def _iface_tick_arrays(plan, analyses):
    """Interface static arrays an authored Update walks — iface → (method, fields).

    ``GameManager.Update`` iterates ``updatables`` calling ``DoUpdate`` on
    each element. That loop does not lower: an element is an interface
    reference, not an index, and C has no virtual call to make on it. The
    pack runs the dispatch itself instead, from ``engine_tick``.

    Only an interface some authored Update-family method actually ticks
    this way gets one; an array that is merely declared is left alone, so
    nothing calls a method the project never called each frame.
    """
    out = {}
    arrays = [(c, f, e) for c, f, e, iface in _any_static_ref_arrays(plan)
              if iface]
    ifaces = plan.get("interfaces") or {}
    for iface in sorted({e for _c, _f, e in arrays}):
        fields = [(c, f) for c, f, e in arrays if e == iface]
        for decl in (ifaces.get(iface) or {}).get("methods") or []:
            if (decl.get("ret") or "void") != "void":
                continue
            if (decl.get("args") or "").strip():
                continue
            if _authored_iface_tick(decl["name"], fields, analyses):
                out[iface] = (decl["name"], fields)
                break
    return out


def _drop_iface_tick_loops(text, plan, site):
    """The authored loop `_iface_tick_arrays` runs from `engine_tick`
    (`for (..i < updatables.Length..) { IUpdatable u = updatables[i];
    u.DoUpdate(); }`) leaves the Update body, so the rest of it lowers.
    ponytail: the dispatch runs after every Update, not at the loop's
    place in this one; inline the switch if a body depends on that order."""
    if (site or {}).get("method") not in ("Update", "FixedUpdate",
                                          "LateUpdate"):
        return text
    ifaces = plan.get("interfaces") or {}
    for _c, f, elem, iface in _any_static_ref_arrays(plan):
        if not iface:
            continue
        void0 = {d["name"] for d in (ifaces.get(elem) or {}).get("methods")
                 or [] if (d.get("ret") or "void") == "void"
                 and not (d.get("args") or "").strip()}
        arr = r"(?:\w+\s*\.\s*)?%s" % re.escape(f)
        text = cs2cpp.code_sub(
            r"for\s*\(\s*int\s+(\w+)\s*=\s*0\s*;\s*\1\s*<\s*(%s)\s*\.\s*"
            r"(?:Length|Count)\s*;\s*\1\s*\+\+\s*\)\s*\{\s*%s\s+(\w+)\s*=\s*"
            r"%s\s*\[\s*\1\s*\]\s*;\s*\3\s*\.\s*(\w+)\s*\(\s*\)\s*;\s*\}"
            % (arr, re.escape(elem), arr),
            lambda m: ("/* %s: engine_tick dispatches */" % m.group(4)
                       if m.group(4) in void0 else m.group(0)), text)
    return text


def _authored_iface_tick(mname, fields, analyses):
    """True if an authored Update-family body loops a field calling *mname*."""
    names = {f for _c, f in fields}
    for a in analyses or []:
        for c in a.get("classes") or []:
            for m in c.get("methods") or []:
                if m.get("name") not in ("Update", "FixedUpdate", "LateUpdate"):
                    continue
                body = cs2cpp._blank(m.get("body") or "")
                # `updatable.DoUpdate ()` — the call is on a receiver.
                if not re.search(r"(?<![\w])%s\s*\(" % re.escape(mname), body):
                    continue
                if any(re.search(r"(?<![\w.])%s\b" % re.escape(n), body)
                       for n in names):
                    return True
    return False


def _rewrite_mb_static_and_singleton(text, plan, cl):
    """``Other.StaticMethod(`` / ``Other.Instance`` → packed C.

    ``Type.Instance`` / ``Type.instance`` → ``Type_Instance()`` (live
    FindObjectOfType cache), not a hardcoded slot. ``Instance.field`` uses
    that index. Inside a singleton type, bare ``instance = this`` →
    ``Type_instance = i`` (static cache field); bare ``Instance`` →
    ``Type_Instance()``; bare ``instance`` reads → ``Type_instance``.
    """
    this = cl.get("name")
    methods_by = plan.get("_methods_by") or {}
    singleton_types = set(plan.get("singleton_instance_types") or ())
    for ocname, ocl in sorted((plan.get("classes") or {}).items(),
                              key=lambda kv: -len(kv[0])):
        oidn = _c_ident(ocname)
        inst = "%s_Instance()" % oidn
        use_inst = ocname in singleton_types or ocname in (
            plan.get("findobject_types") or ())
        # Other.Instance.field / Other.instance.field
        # Writes before reads — otherwise `Instance.f = v` becomes
        # `Class_get_f(Class_Instance()) = v` (not an lvalue → CS0000).
        for vf in ocl.get("vec2_fields") or []:
            text = cs2cpp.code_sub(
                r"(?<![\w.])%s\s*\.\s*(?:Instance|instance)\s*\.\s*%s\s*=(?!=)\s*"
                r"([^;]+);"
                % (re.escape(ocname), re.escape(vf)),
                lambda m, o=oidn, f=vf, ix=inst: (
                    "{ Vector2 _v2 = (%s); %s_set_%s_x(%s, _v2.x); "
                    "%s_set_%s_y(%s, _v2.y); }"
                    % (m.group(1), o, f, ix, o, f, ix)),
                text)
            text = cs2cpp.code_sub(
                r"(?<![\w.])%s\s*\.\s*(?:Instance|instance)\s*\.\s*%s\b"
                % (re.escape(ocname), re.escape(vf)),
                "Vector2_make(%s_get_%s_x(%s), %s_get_%s_y(%s))"
                % (oidn, vf, inst, oidn, vf, inst),
                text)
        for f in sorted((ocl.get("class_consts") or []),
                        key=lambda f: -len(f["name"])):
            if _is_scalar_static(f) or _is_ref_static(f, plan, ocname):
                text = cs2cpp.code_sub(
                    r"(?<![\w.])%s\s*\.\s*%s\b(?!\s*\()"
                    % (re.escape(ocname), re.escape(f["name"])),
                    "%s_%s" % (oidn, f["name"]), text)
            if not _is_ref_static(f, plan, ocname):
                continue
            sym = "%s_%s" % (oidn, f["name"])
            if ocname == this:
                text = cs2cpp.code_sub(
                    r"(?<![\w.])(?:this\s*\.\s*)?%s\b(?!\s*\()"
                    % re.escape(f["name"]), sym, text)
            # an index: null is -1. ponytail: a destroyed object still reads
            # non-null (Unity's == null is true); clear it in OnDestroy if
            # that matters
            text = cs2cpp.code_sub(r"(?<![\w.])%s\s*==\s*null\b" % sym,
                                   "%s < 0" % sym, text)
            text = cs2cpp.code_sub(r"(?<![\w.])%s\s*!=\s*null\b" % sym,
                                   "%s >= 0" % sym, text)
            text = cs2cpp.code_sub(r"(?<![\w.])%s\s*=\s*null\b" % sym,
                                   "%s = -1" % sym, text)
        member_names = {n for n, _t, _b, _k in (ocl.get("members") or [])}
        for mem in sorted(member_names, key=len, reverse=True):
            if mem.endswith("_x") or mem.endswith("_y") or mem.endswith("_z"):
                continue
            if mem.startswith("pos_"):
                continue
            text = cs2cpp.code_sub(
                r"(?<![\w.])%s\s*\.\s*(?:Instance|instance)\s*\.\s*%s\s*=(?!=)\s*"
                r"([^;]+);"
                % (re.escape(ocname), re.escape(mem)),
                "%s_set_%s(%s, (\\1));" % (oidn, mem, inst),
                text)
            text = cs2cpp.code_sub(
                r"(?<![\w.])%s\s*\.\s*(?:Instance|instance)\s*\.\s*%s\b"
                % (re.escape(ocname), re.escape(mem)),
                "%s_get_%s(%s)" % (oidn, mem, inst),
                text)
        for f in ocl.get("ref_array_fields") or []:
            fname = f["name"]
            # cpprust lowers members of a vector name, not of `arr[i]`: the
            # row's vector is bound to a reference, as instance lists are.
            pat = (r"(?<![\w.])%s\s*\.\s*(?:Instance|instance)\s*\.\s*%s\b"
                   % (re.escape(ocname), re.escape(fname)))
            alias = "_%s_%s_of" % (oidn, fname)
            if ocname == this:
                text = cs2cpp.code_sub(pat, fname, text)
                pat, alias = (r"(?<![\w.])(?:this\s*\.\s*)?%s\b"
                              % re.escape(fname)), fname
                bind = "std::vector<int> &%s = %s_%s[i];" % (alias, oidn,
                                                            fname)
            else:
                bind = "std::vector<int> &%s = %s_%s[%s];" % (
                    alias, oidn, fname, inst)
            if not re.search(pat, cs2cpp._blank(text)):
                continue
            text = cs2cpp.code_sub(pat + r"\s*\.\s*Length\b",
                                   "(int)%s.size()" % alias, text)
            # a whole-array store goes to the row: cpprust cannot assign
            # through the reference
            text = cs2cpp.code_sub(pat + r"(?=\s*=(?!=))", "%s_%s[%s]" % (
                oidn, fname, "i" if ocname == this else inst), text)
            # same line: the body's lines stay the C# lines (NRE sites)
            text = bind + " " + cs2cpp.code_sub(pat, alias, text)
        # Other.Instance / Other.instance — including Other.Instance.unknownField
        # (known .field patterns already rewritten above). Always emit the
        # live finder call; do not leave `Type.instance.` for crust.
        if use_inst or ocname in (plan.get("classes") or {}):
            text = cs2cpp.code_sub(
                r"(?<![\w.])%s\s*\.\s*(?:Instance|instance)\s*\.\s*enabled\s*"
                r"=(?!=)\s*([^;]+);" % re.escape(ocname),
                lambda m, o=oidn, n=inst: "%s_set_enabled(%s, (int)(%s));" % (
                    o, n, m.group(1)), text)
            text = cs2cpp.code_sub(
                r"(?<![\w.])%s\s*\.\s*(?:Instance|instance)\s*\.\s*enabled\b"
                r"(?!\s*=(?!=))" % re.escape(ocname),
                "%s_get_enabled(%s)" % (oidn, inst), text)
            text = cs2cpp.code_sub(
                r"(?<![\w.])%s\s*\.\s*(?:Instance|instance)\b"
                % re.escape(ocname),
                inst, text)
    # Bare singleton field on *this* class (GameCamera Awake: instance = this).
    # Only when Type_instance / Type_Instance() are emitted.
    if this and this in singleton_types:
        oidn = _c_ident(this)
        text = cs2cpp.code_sub(
            r"(?<![\w.])[Ii]nstance\s*=(?!=)",
            "%s_instance =" % oidn, text)
        text = cs2cpp.code_sub(
            r"(?<![\w.])Instance\b(?!\s*\()",
            "%s_Instance()" % oidn, text)
        text = cs2cpp.code_sub(
            r"(?<![\w.])instance\b",
            "%s_instance" % oidn, text)
    # FindObjectOfType<T>() / FindObjectOfType<T>(bool)
    for tname in sorted(plan.get("findobject_types") or (),
                        key=len, reverse=True):
        if tname not in (plan.get("classes") or {}):
            continue
        oidn = _c_ident(tname)
        text = cs2cpp.code_sub(
            r"(?:UnityEngine\.)?(?:Object\.)?FindObjectOfType\s*<\s*%s\s*>"
            r"\s*\(\s*\)" % re.escape(tname),
            "Object_FindObjectOfType_%s(0)" % oidn, text)
        text = cs2cpp.code_sub(
            r"(?:UnityEngine\.)?(?:Object\.)?FindObjectOfType\s*<\s*%s\s*>"
            r"\s*\(\s*true\s*\)" % re.escape(tname),
            "Object_FindObjectOfType_%s(1)" % oidn, text)
        text = cs2cpp.code_sub(
            r"(?:UnityEngine\.)?(?:Object\.)?FindObjectOfType\s*<\s*%s\s*>"
            r"\s*\(\s*false\s*\)" % re.escape(tname),
            "Object_FindObjectOfType_%s(0)" % oidn, text)
        text = cs2cpp.code_sub(
            r"(?:UnityEngine\.)?(?:Object\.)?FindObjectsOfType\s*<\s*%s\s*>"
            r"\s*\(\s*(true|false|1|0)?\s*\)" % re.escape(tname),
            lambda m, o=oidn: "Object_FindObjectsOfType_%s(%d)" % (
                o, m.group(1) in ("true", "1")), text)
    for ocname, pairs in methods_by.items():
        oidn = _c_ident(ocname)
        overloaded = _overload_method_names([m for _c, m in pairs])
        for _c, m in pairs:
            if not m.get("static"):
                continue
            mname = m["name"]
            # Overloads need arg-type dispatch; leave unlowered → stub.
            if mname in overloaded:
                continue
            sym = _method_c_symbol(
                oidn, mname, m.get("args") or "", False)
            text = re.sub(
                r"(?<![\w.])%s\s*\.\s*%s\s*\(" % (
                    re.escape(ocname), re.escape(mname)),
                "%s(" % sym, text)
            if ocname == this:
                text = re.sub(
                    r"(?<![\w.])%s\s*\(" % re.escape(mname),
                    "%s(" % sym, text)
            text = _fill_defaults_after(
                text, sym, cs2cpp.parse_params(m.get("args") or ""))
        text = _lower_static_properties(text, plan, ocname, pairs, overloaded,
                                        ocname == this)
    return text


def _lower_static_properties(text, plan, ocname, pairs, overloaded, own):
    """A static property of *ocname* used by name -- `Type.Name`, or bare
    `Name` inside the type -- as the `get_Name` / `set_Name` functions
    `properties_as_methods` made: `Name = x;` the setter, `Name += x;` /
    `Name++;` the setter of the getter's value, a read the getter (when its
    type has a C value)."""
    oidn = _c_ident(ocname)
    acc = {m["name"]: m for _c, m in pairs
           if m.get("static") and m["name"][:4] in ("get_", "set_")
           and m["name"] not in overloaded
           and m["name"][4:] not in ("Instance", "instance")}
    for prop in sorted({n[4:] for n in acc}, key=len, reverse=True):
        get, put = acc.get("get_" + prop), acc.get("set_" + prop)
        if get is not None and _ret_c_ty(get.get("ret"), plan) in (None, "void"):
            get = None
        recv = r"(?<![\w.])(?:%s\s*\.\s*%s%s)" % (
            re.escape(ocname), re.escape(prop),
            r"|(?<![\w.])%s" % re.escape(prop) if own else "")
        gsym = "%s_get_%s()" % (oidn, prop)
        if put is not None:
            ssym = "%s_set_%s" % (oidn, prop)
            if get is not None:
                text = cs2cpp.code_sub(
                    r"(?:%s)\s*([-+*/])=\s*([^;]+);" % recv,
                    lambda m, s=ssym, g=gsym: "%s(%s %s (%s));" % (
                        s, g, m.group(1), m.group(2).strip()), text)
                text = cs2cpp.code_sub(
                    r"(?:(?:%s)\s*(\+\+|--)|(\+\+|--)\s*(?:%s))\s*;"
                    % (recv, recv),
                    lambda m, s=ssym, g=gsym: "%s(%s %s 1);" % (
                        s, g, (m.group(1) or m.group(2))[0]), text)
            text = cs2cpp.code_sub(
                r"(?:%s)\s*=(?!=)\s*([^;]+);" % recv,
                lambda m, s=ssym: "%s(%s);" % (s, m.group(1).strip()), text)
        if get is not None:
            text = cs2cpp.code_sub(
                r"(?:%s)\b(?!\s*(?:\(|[-+*/]?=(?!=)|\+\+|--))" % recv,
                gsym, text)
    return text



def _unstored_field_names(cl, plan):
    """Authored fields -- the class's and its bases' -- with no packed slot.

    A `Transform` reference, a uGUI handle, an instance `string`, a field
    declared on a base class (inheritance is not modelled: a base is its
    own packed array): the struct has no member for any of them.
    """
    stored = {n for n, _ty, _bits, _kind in cl.get("members") or []}
    for key in ("class_consts", "dict_fields", "list_fields",
                "ref_array_fields"):
        stored |= {f["name"] for f in cl.get(key) or []}
    fields = list(cl.get("fields") or [])
    bases = plan.get("mb_bases") or {}
    classes = plan.get("classes") or {}
    seen = {cl.get("name")}
    queue = list(bases.get(cl.get("name")) or [])
    while queue:
        b = queue.pop()
        if b in seen:
            continue
        seen.add(b)
        fields.extend((classes.get(b) or {}).get("fields") or [])
        queue.extend(bases.get(b) or [])
    out = set()
    for f in fields:
        name = f.get("name")
        if not name or name in stored:
            continue
        # Vector2 / Vector3 fields are stored as their _x/_y/_z components.
        if any(("%s_%s" % (name, ax)) in stored for ax in ("x", "y", "z")):
            continue
        out.add(name)
    return out


def _writes_embedded_struct(body, plan):
    """The first write to a field of an embedded [Serializable] struct's row
    in lowered *body*, else None (see `_embedded_struct_rows`)."""
    for s in sorted(plan.get("embedded_structs") or ()):
        sid = re.escape(_c_ident(s))
        m = re.search(r"(?<![\w])%s_set_\w+\s*\(|(?<![\w])%s_AT\s*\([^;]*?\)"
                      r"\s*\.\s*\w+\s*(?:[-+*/%%&|^]?=(?!=)|\+\+|--)"
                      % (sid, sid), body)
        if m:
            return m.group(0)
    return None


def _writes_unstored_field(body, cl, plan):
    """The `this.f = v` of a field the pack keeps no storage for, or None.

    Read on the authored C#, where `this.` says field and not local, so a
    parameter of the same name cannot be mistaken for one. There is no C
    name to assign, so the method reports as unlowered instead of emitting
    a dangling identifier.
    """
    unstored = _unstored_field_names(cl, plan)
    if not unstored:
        return None
    scan = cs2cpp._blank(body or "")
    for m in re.finditer(r"(?<![\w.])this\s*\.\s*(\w+)\s*=(?!=)", scan):
        if m.group(1) in unstored:
            return (body or "")[m.start():m.end()]
    # a delegate field (an event, an Action) used at all: no C name either
    for f in cl.get("fields") or []:
        if f.get("delegate") and f["name"] in unstored:
            m = re.search(r"(?<![\w.])(?:this\s*\.\s*)?%s\b" % re.escape(
                f["name"]), scan)
            if m:
                return (body or "")[m.start():m.end()]
    return None


def _class_field_names(cl):
    """Every name a body of `cl` can mean a field by (packed, static, authored)."""
    names = {n for n, _ty, _bits, _kind in cl.get("members") or []}
    names |= {f["name"] for f in cl.get("fields") or []}
    names |= {f["name"] for f in cl.get("class_consts") or []}
    return names


def _rename_shadowed_params(cl, m):
    """A parameter that shadows a field of `cl` (`this.x = x`), renamed.

    C# tells the two apart by `this.`, which the packed lowering strips
    before fields become accessors -- so a constructor's `this.scrollbar =
    scrollbar` read as the field assigned to itself. The emitted C tells
    them apart by name instead: the parameter becomes `scrollbar_` in both
    the signature and the body, and the field keeps the authored name.
    """
    args = m.get("args") or ""
    if not args.strip():
        return m
    fields = _class_field_names(cl)
    if not fields:
        return m
    body = m.get("body") or ""
    renamed = False
    for part in args.split(","):
        part = re.sub(r"\b(?:ref|out|in|params)\s+", "", part.strip())
        pm = re.match(r"([\w.<>]+)\s+(\w+)\s*$", part)
        if not pm or pm.group(2) not in fields:
            continue
        old = pm.group(2)
        new = old + "_"
        while new in fields:
            new += "_"
        # `this.x` keeps the field (the `.` is in the lookbehind); every
        # other mention of the name in the body is the parameter.
        body = cs2cpp.code_sub(r"(?<![\w.])%s\b" % re.escape(old), new, body)
        args = re.sub(r"(?<![\w.])%s\b" % re.escape(old), new, args)
        renamed = True
    if not renamed:
        return m
    out = dict(m)
    out["args"] = args
    out["body"] = body
    return out


def _extra_roots(plan, cname):
    """Methods the scene / the engine calls by name: Animation Events, and
    InputAction callbacks."""
    import tools.unity.unity_pack_input as _inp
    return (set((plan.get("anim_event_methods") or {}).get(cname, ()))
            | set(_inp.ROOTS.get(cname, ())))


def _reachable_emit_methods(methods, extra_roots=()):
    """Methods to lower: Unity messages, public API, and private callees.

    Editor-only helpers (e.g. UpdateCanvas only called from OnValidate) stay out.
    Overloads share a name — all of them are kept when the name is reachable.
    """
    by_name = {}
    for m in methods or []:
        by_name.setdefault(m["name"], []).append(m)
    # and what the scene calls by name: an Animation Event's function
    roots = {n for n in extra_roots if n in by_name}
    for name, ms in by_name.items():
        if name == "OnValidate":
            continue
        if name in _UNITY_EMIT_MESSAGES or any(m.get("public") for m in ms):
            roots.add(name)
    reach = set(roots)
    queue = list(roots)
    while queue:
        name = queue.pop()
        for m in by_name.get(name) or []:
            body = m.get("body") or ""
            for other in by_name:
                if other in reach:
                    continue
                # a property's accessor: by the property's own name
                pat = (r"(?<![\w.])%s\b" % re.escape(other[4:])
                       if other[:4] in ("get_", "set_")
                       else r"(?<![\w.])%s\s*\(" % re.escape(other))
                if re.search(pat, body):
                    reach.add(other)
                    queue.append(other)
    return reach


def _lowered_body_still_csharp(body, args_str=None, emitted_params=None):
    """True if *body* still has C# the C subset cannot parse (see below)."""
    return _unlowered_csharp(body, args_str, emitted_params) is not None


def _engine_types_declared(lines):
    """C type names the engine text so far defines (`} Name;`, typedefs)."""
    text = "\n".join(lines)
    names = set(re.findall(r"(?m)^\}\s*([A-Za-z_]\w*)\s*;", text))
    names.update(re.findall(r"typedef\s+struct\s+([A-Za-z_]\w*)", text))
    # C++-subset structs (`struct Vector2Int { .. }`, for map keys).
    names.update(re.findall(r"(?m)^\s*struct\s+([A-Za-z_]\w*)\s*\{", text))
    names.update(re.findall(r"typedef\s+[^;{}]*?\b([A-Za-z_]\w*)\s*;", text))
    return names


def _unlowered_csharp(body, args_str=None, emitted_params=None,
                      known_types=(), properties=()):
    """What is left in a lowered body that the C subset cannot take, or None.

    Returns (what, text): the check that fired and the source text it
    matched, for the stub diagnostic `emit_engine` reports.

    The Unity questions are asked here -- an `Instantiate` overload or
    `GetComponents<T>` nothing lowered, a `Type.instances` array, a
    component's `.gameObject` or `.activeSelf`, a parameter the emitter did
    not make a C formal. The C# ones -- arrays, generic calls, lambdas,
    calls, statics, member access, typed locals nothing lowered -- are
    cs2cpp's (`residual_csharp`), told what is this engine's own C.
    """
    if not body or not str(body).strip():
        return None
    raw = body
    scan = cs2cpp._blank(body)

    def unity(pattern, what):
        m = re.search(pattern, scan)
        return (what, raw[m.start():m.end()]) if m else None

    for pattern, what in (
            (r"(?<![\w.])(?:Object\.)?Instantiate\s*\(",
             "Instantiate(this[, parent]) is rewritten; leftover overloads "
             "still stub."),
            (r"GetComponentsInChildren\s*<",
             "GetComponentsInChildren is rewritten; bare GetComponents (no "
             "InChildren) stubs."),
            (r"GetComponents\s*<",
             "GetComponentsInChildren is rewritten; bare GetComponents (no "
             "InChildren) stubs."),
            (r"(?<![\w._])[A-Z]\w*\.instances\b",
             "Static `Type.instances` array not lowered."),
            (r"\w+\.gameObject\b",
             "Unity component handle still using `recv.gameObject`."),
            (r"\w+\.activeSelf\b",
             "Unity `activeSelf` on a receiver nothing lowered."),
            (r"(?<![\w.])enabled\b",
             "Behaviour `enabled` nothing lowered (a script writes it, or a "
             "row was authored disabled)."),
            # ponytail: cpprust copies a string through a pointer shallowly
            (r"_engine_map_at_si_std_string\s*\(",
             "A string-valued Dictionary element (`d[key]`) is not lowered "
             "yet.")):
        hit = unity(pattern, what)
        if hit:
            return hit
    # A C# property the pack does not answer: only setters lower, so a name
    # left standing is a read (`Equipped = Equipped`), not C.
    for prop in sorted(properties or ()):
        hit = unity(r"(?<![\w.])%s(?![\w])" % re.escape(prop),
                    "C# property read (only `set_Name` lowers, for "
                    "UnityEvent wiring).")
        if hit:
            return hit
    # Instance method C# params not emitted as C formals (only ``i`` / coll).
    emitted = set(emitted_params or ()) | {"i"}
    for part in (args_str or "").split(","):
        part = part.strip()
        if not part:
            continue
        part = re.sub(r"\b(?:ref|out|in|params)\s+", "", part)
        pm = re.match(r"([\w.<>]+)\s+(\w+)\s*$", part)
        if not pm or pm.group(2) in emitted:
            continue
        hit = unity(r"(?<![\w.])%s\b" % re.escape(pm.group(2)),
                    "Instance method C# params not emitted as C formals "
                    "(only `i` / coll).")
        if hit:
            return hit
    # A parameter of one of the engine's value types (`Vector2 p`) is a C
    # struct in the signature: `p.x` is its member, as a local's would be.
    # Declared here only for the check, which reads declarations from the
    # body it is given.
    pre = ""
    for prm in cs2cpp.parse_params(args_str or ""):
        t = prm.type.split(".")[-1]
        if t in known_types and _param_c_ty(t) == t:
            pre += "%s %s; " % (t, prm.name)
    return cs2cpp.residual_csharp(pre + body, _PACKED_STRINGS, known_types,
                                  _UNITY_VALUE_CTORS)


#: UnityEngine value types kept in a lowered body as constructor calls.
_UNITY_VALUE_CTORS = ("Vector2Int", "Vector3Int", "Vector4", "Vector3",
                      "Vector2", "Color", "Quaternion", "RectInt", "Rect",
                      "Bounds")


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


def _f16_exact(v):
    """Does float *v* survive a binary16 field unchanged (`damage: Infinity`,
    `1e-05`, `0.1` do not)?"""
    import math
    import struct
    try:
        f = float(v)
        return math.isfinite(f) and struct.unpack("<e", struct.pack("<e", f))[0] == f
    except (OverflowError, TypeError, ValueError):
        return False


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


_INT_WRITE_OPS = r"(?:\+\+|--|[-+*/%&|^]=|<<=|>>=|=(?!=))"


def _int_field_writes_unbounded(fname, methods, all_methods=()):
    """Whether any write to `fname` is not a literal or a const.

    A bitfield is only sound when every value it will hold is known: the
    scene's and the literals the scripts assign (`_assigned_int_seeds`).
    `seen = target.hp`, `hp++` and `hp += n` bound nothing, and the field
    silently truncated or wrapped -- `seen = target.hp` with hp 5 stored 1
    in the 1-bit field the scene's 0 had chosen. So a field written that
    way keeps its C# width. `all_methods` adds every script's methods, for
    writes through another object (`other.fname = ..`, via a handle field).
    """
    own = r"(?<![\w.])%s" % re.escape(fname)
    other = r"\.\s*%s" % re.escape(fname)
    for pat, bodies in ((own, methods), (other, all_methods)):
        for m in bodies or ():
            body = cs2cpp._blank(m.get("body") or "")
            if re.search(r"(?:\+\+|--)\s*%s(?![\w])" % pat, body):
                return True
            for wm in re.finditer(r"%s\s*(%s)" % (pat, _INT_WRITE_OPS), body):
                op = wm.group(1)
                if op != "=":
                    return True
                rhs = body[wm.end():].split(";", 1)[0].strip()
                if rhs == fname or re.match(r"-?\d+$", rhs):
                    continue
                if re.match(r"[A-Z_][A-Z0-9_]*$", rhs):
                    continue        # a const: `_assigned_int_seeds` seeds it
                return True
    return False


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


def _embedded_struct_rows(by_class, analyses):
    """Rows for the [Serializable] values objects embed (`public
    AnimationEntry jump;`, authored as a nested mapping): each becomes a row
    of its type's packed class, and the owner's field its index, as a
    reference to another packed object is. Two copies of a value then share
    a row, so the rows are read-only (`_writes_embedded_struct` refuses a
    write): sharing a value nobody changes is a copy.
    ponytail: an owner the scene does not author (AddComponent) holds null,
    not default(T); a shared default row if that matters."""
    info = {c["name"]: c for a in analyses for c in a.get("classes") or []}
    embedded, serial = set(), itertools.count()
    queue = [(cn, o) for cn in sorted(by_class) for o in by_class[cn]]
    while queue:
        cname, o = queue.pop(0)
        sv = o.get("struct_values") or {}
        if not sv or cname not in info:
            continue
        for f in info[cname].get("fields") or []:
            ty = (f.get("ty") or "").split(".")[-1]
            t = info.get(ty)
            if (f["name"] not in sv or f.get("static") or not t
                    or t.get("engine_derived", bool(t.get("bases")))):
                continue
            v = sv[f["name"]]
            rid = "embedded:%d" % next(serial)
            row = {"name": "%s.%s" % (o.get("name") or cname, f["name"]),
                   "class": ty, "pos": (0.0, 0.0, 0.0),
                   "rot": (0.0, 0.0, 0.0, 1.0), "active": 1,
                   "fields": dict(v["fields"]),
                   "str_fields": dict(v["str_fields"]),
                   "object_refs": dict(v["object_refs"]),
                   "vec2_fields": dict(v["vec2_fields"]),
                   "struct_values": v.get("struct_values") or {},
                   "mb_ids": [rid], "embedded": True}
            o.setdefault("object_refs", {})[f["name"]] = rid
            by_class.setdefault(ty, []).append(row)
            embedded.add(ty)
            queue.append((ty, row))
    return embedded


def plan_layouts(objects, analyses, two_d=None):
    """Per-class packed field list + index width."""
    by_class = {}
    for o in objects:
        by_class.setdefault(o["class"], []).append(o)

    # GetComponent / FindObjectOfType / MB field targets that lost every
    # authored instance (trimmed assets) still need a packed class (n=0) so
    # rewrite emits GameObject_GetComponent_* instead of CS0246.
    analyzed = set()
    for a in analyses:
        for c in a.get("classes") or []:
            analyzed.add(c["name"])
    referenced = set()
    for a in analyses:
        referenced |= set(a.get("getcomponent_types") or [])
        referenced |= set(a.get("getcomponentsinchildren_types") or [])
        referenced |= set(a.get("addcomponent_types") or [])
        referenced |= set(a.get("findobject_types") or [])
        referenced |= set(a.get("singleton_instance_types") or [])
        for c in a.get("classes") or []:
            if a.get("static_ref"):
                referenced.add(c["name"])
            for f in c.get("fields") or []:
                ty = f.get("ty") or ""
                if ty in analyzed:
                    referenced.add(ty)
            for b in c.get("bases") or []:
                if b in analyzed:
                    referenced.add(b)
    skip = (_ADDABLE_BUILTINS | _PHYSICS_COMPONENTS | (_JOINT2D_COMPONENTS | _PARTICLE_COMPONENTS)
            | _UI_GETCOMPONENT_TYPES
            | _UI_COMPONENT_FIELD_TYPES | _TRANSFORM_GETCOMPONENT_TYPES)
    for t in referenced:
        if t in analyzed and t not in skip:
            by_class.setdefault(t, [])

    embedded = _embedded_struct_rows(by_class, analyses)
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
    # SetParent / DetachChildren / a Transform handle's position move the
    # rows script fields name, and DetachChildren their children too.
    if any("transform.SetParent" in (a.get("apis") or ()) for a in analyses):
        refs = {str(f) for o in objects
                for f in list((o.get("object_refs") or {}).values())
                + [x for arr in (o.get("object_ref_arrays") or {}).values()
                   for x in arr]}
        for cname, insts in by_class.items():
            if any({str(o.get("xf_id")), str(o.get("go_id")),
                    str(o.get("father_id"))} & refs for o in insts):
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

    max_inst = {}
    for a in analyses:
        for c in a.get("classes") or []:
            if c.get("max_instances") is not None:
                max_inst[c["name"]] = c
    # A Godot template's row is dormant, not a live instance: it raises a
    # [MaxInstances(N)] class's rows by one each, N still the live cap.
    for cname, insts in by_class.items():
        tmpl = sum(1 for o in insts
                   if o.get("godot_template") and not o.get("godot_pool"))
        if tmpl and max_inst.get(cname) is not None:
            max_inst[cname] = dict(max_inst[cname], max_instances=int(
                max_inst[cname]["max_instances"]) + tmpl)
    widths = dict((cname, _class_index_width(cname, len(insts), spawn,
                                              max_inst.get(cname)))
                  for cname, insts in by_class.items())

    plans = {}
    for cname, insts in by_class.items():
        n = len(insts)
        idx_ty, idx_bits, bounded = widths[cname]

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
                # Vector2 / Vector2Int components are packed via the script field.
                if k.endswith("_x") or k.endswith("_y"):
                    base = k[:-2]
                    if any(f["name"] == base and f.get("ty") in (
                            "Vector2", "Vector2Int")
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

        delegates = {f["name"] for f in script_fields if f.get("delegate")}
        for fname, ty in field_tys.items():
            if ty == "string":
                # Instance strings are not packed yet (static strings are).
                continue
            if fname in delegates:
                # an event / Action: no storage; a body using it stubs
                continue
            if ty in ("StreamWriter", "StreamReader"):
                # Text streams are FILE* class/static fields, not instance slots.
                continue
            if ty == "Vector2":
                members.append((fname + "_x", "float", 32, "f32"))
                members.append((fname + "_y", "float", 32, "f32"))
                continue
            if ty == "Rect":
                # its four floats; `Cls_get_f(i)` is the whole Rect
                for ax in _RECT_AXES:
                    members.append((fname + "_" + ax, "float", 32, "f32"))
                continue
            if ty == "Vector2Int":
                members.append((fname + "_x", "int", 32, "i32"))
                members.append((fname + "_y", "int", 32, "i32"))
                continue
            if ty == "Vector3":
                # Pack when TransformPoint / localPosition needs field points.
                if cname in vec3_pack_classes:
                    members.append((fname + "_x", "float", 32, "f32"))
                    members.append((fname + "_y", "float", 32, "f32"))
                    members.append((fname + "_z", "float", 32, "f32"))
                continue  # otherwise transform owns position; full Vector3 later
            if ty in _GO_HANDLE_FIELD_TYPES:
                # A Transform / uGUI / TMP reference names a component, and
                # every engine UI helper addresses one by its GameObject
                # (Transform ≡ RectTransform ≡ GameObject index here). The
                # scene's fileID resolves to that GO in `_resolve_go_field_refs`.
                members.append((fname, "uint32_t", 32, "go"))
                continue
            if _array_elem_name(ty):
                # Toggle[] / MB[] — parallel std::vector<int> of GO / inst idxs.
                continue
            if ty == "AudioClip":
                # AudioClip assets — opaque handles later; skip pack.
                continue
            if _list_elem_name(ty):
                # Instance List<T> not packed in AoS; static Lists are class_consts.
                continue
            if _dict_kv_names(ty):
                # Instance Dictionary — parallel std::map tables, not AoS slots.
                continue
            if ty in ("int", "byte", "short", "uint"):
                vals = [o["fields"][fname] for o in insts if fname in o["fields"]]
                vals.extend(_assigned_int_seeds(
                    fname, script_methods, script_fields))
                type_bits = _CS_INT_BITS.get(ty, 32)
                # No scene/seed values → C# width (not phantom [0] → 1 bit).
                # Seeds from `framesLeft = FRAME_CNT` widen counters correctly.
                # A write that is not a literal bounds nothing: C# width.
                # JsonUtility.FromJsonOverwrite writes any serialized field
                # a value no literal bounds.
                json_writes = any(
                    "JsonUtility.FromJsonOverwrite" in (a.get("apis") or ())
                    for a in analyses)
                if not vals or json_writes or _int_field_writes_unbounded(
                        fname, script_methods,
                        [mm for a in analyses for c in a.get("classes") or []
                         for mm in c.get("methods") or []]):
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
                dflt = next((f.get("default") for f in script_fields
                             if f["name"] == fname), None)
                if static and all(_f16_exact(v) for v in [dflt or 0] + [
                        o["fields"][fname] for o in insts if fname in o["fields"]]):
                    members.append((fname, "uint16_t", 16, "f16"))
                else:
                    members.append((fname, "float", 32, "f32"))
            elif ty == "LineRenderer":
                members.append((fname, "int", 32, "idx:LineRenderer"))
            elif ty in ("AnimationCurve", "Curve"):     # Godot's Curve too
                # A row of the scene's curve table (unity_pack_curves.py),
                # full width: it indexes every curve, not this class's.
                members.append((fname, "int", 32, "idx:AnimationCurve"))
            elif ty == "string":
                # Not a struct member: a table of owned strings beside the
                # instance array (`string_fields`). It used to fall through
                # to the handle case below, as an index into a class named
                # `string`, and never declared at all.
                continue
            elif ty in _COLLIDER2D_FIELD_TYPES:
                # a row of the collider table (GetComponent<Collider2D>'s)
                members.append((fname, "uint32_t", 32, "idx:" + ty))
            elif ty == "Sprite":
                # a texture index (`_sprite_ref_textures`)
                members.append((fname, "uint32_t", 32, "idx:Sprite"))
            else:
                # Foreign MonoBehaviour → index into that class's array, as
                # wide as *that* class's index: the owner's could be
                # narrower and truncate it.
                t_ty, t_bits, _tb = widths.get(ty, (idx_ty, idx_bits, bounded))
                members.append((fname, t_ty, t_bits, "idx:" + ty))

        # Size with C bitfield packing (same word until 32 bits).
        size = _packed_size(members)
        vec2_fields = [f["name"] for f in script_fields if f["ty"] == "Vector2"]
        vec2int_fields = [f["name"] for f in script_fields
                          if f["ty"] == "Vector2Int"]
        vec3_fields = [f["name"] for f in script_fields
                       if f["ty"] == "Vector3" and cname in vec3_pack_classes]
        class_consts = [f for f in script_fields
                        if f.get("const") or f.get("static")]
        dict_fields = [
            f for f in script_fields
            if not f.get("static") and not f.get("const")
            and _dict_kv_names(f.get("ty") or "")
        ]
        list_fields = [
            f for f in script_fields
            if not f.get("static") and not f.get("const")
            and _list_elem_name(f.get("ty") or "")
        ]
        # a number array (`float[] vols`) is stored as a list of its numbers,
        # seeded with the authored ones; `array` marks the C# array
        list_fields += [
            dict(f, ty="List<%s>" % _array_elem_name(f["ty"]), array=True)
            for f in script_fields
            if not f.get("static") and not f.get("const")
            and _array_elem_name(f.get("ty") or "") in _NUMBER_ARRAY_ELEMS]
        # a vector of indices: an array of values (`string[]`) has no slot
        ref_array_fields = [
            f for f in script_fields
            if not f.get("static") and not f.get("const")
            and _array_elem_name(f.get("ty") or "")
            and _array_elem_name(f.get("ty") or "") not in _VALUE_ELEM_TYPES
        ]
        # Instance `string` fields: a fastring table each, seeded per
        # instance from the scene's value, else the field's initializer.
        string_fields = []
        for f in script_fields:
            if f.get("static") or f.get("const") or f.get("ty") != "string":
                continue
            d = f.get("default")
            d = d if isinstance(d, str) else ""
            string_fields.append({
                "name": f["name"],
                "values": [_authored_string(o, f["name"], d) for o in insts],
                "default": d,
            })
        # Do not bake Application.* paths used in illegal field initializers.
        if ctor_forbidden:
            forbid_names = {x["field"] for x in ctor_forbidden}
            class_consts = [f for f in class_consts
                            if f["name"] not in forbid_names]
        plans[cname] = {
            "name": cname,
            "n": n,
            "max_instances": (max_inst[cname]["max_instances"]
                              if cname in max_inst else None),
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
            "vec2int_fields": vec2int_fields,
            "vec3_fields": vec3_fields,
            "class_consts": class_consts,
            "dict_fields": dict_fields,
            "list_fields": list_fields,
            "ref_array_fields": ref_array_fields,
            "string_fields": string_fields,
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
        "embedded_structs": sorted(embedded),
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
# C emit: unity_pack_emit.py (moved out whole; it shares this namespace)
# ---------------------------------------------------------------------------

import tools.unity.unity_pack_emit as _emit_mod  # noqa: E402
_EMIT_OWN = set(vars(_emit_mod)) - _emit_mod._INJECTED
globals().update({k: getattr(_emit_mod, k) for k in _EMIT_OWN if not k.startswith("__")})


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------

#: Bases that are never an authored interface — no file worth looking for.
_NOT_AN_INTERFACE_BASE = frozenset((
    "MonoBehaviour", "ScriptableObject", "Object", "object", "System"))


def _interface_names_in_cs(path):
    """Interface names a .cs file declares."""
    try:
        text = _read(path)
    except OSError:
        return []
    if "interface" not in text:
        return []
    return re.findall(r"(?<![\w.])interface\s+(\w+)", cs2cpp._blank(text))


def _analyze_base_interfaces(root, guids, analyses):
    """Analyses holding only the interfaces the scene's classes implement.

    An interface is not a component, so no scene names its file; the one
    reference is `class _Scrollbar : IUpdatable` in a script that is in a
    scene. What the interface declares decides how an `IUpdatable[]`
    packs, so its file has to be read. Nothing else in that file counts
    -- no scene places it, and giving it a packed class here would invent
    an object the project never has.
    """
    wanted = set()
    for a in analyses:
        for c in a.get("classes") or []:
            wanted |= set(c.get("bases") or [])
    wanted -= {c["name"] for a in analyses for c in a.get("classes") or []}
    wanted -= _NOT_AN_INTERFACE_BASE
    if not wanted:
        return []
    done = {os.path.abspath(a.get("path") or "") for a in analyses}
    paths = set()
    for _g, path in (guids or {}).items():
        if not path or not _is_player_csharp(root, path):
            continue
        path = os.path.abspath(path)
        if path in done:
            continue
        if any(n in wanted for n in _interface_names_in_cs(path)):
            paths.add(path)
    out = []
    for path in sorted(paths):
        a = analyze_script(path)
        out.append(dict(a, apis=set(), classes=[], spawns=False,
                        uses_z=False, writes_pos=False, writes_rot=False,
                        literals=[]))
    return out


def _mb_typename_to_script(root, guids):
    """MonoBehaviour class name → authored .cs path under Assets/."""
    out = {}
    for _g, path in (guids or {}).items():
        if not path or not _is_player_csharp(root, path):
            continue
        name = _class_name_from_cs(path)
        if name:
            out.setdefault(name, os.path.abspath(path))
    return out


def _script_guid_for_path(guids, script_path):
    want = os.path.abspath(script_path)
    for g, path in (guids or {}).items():
        if path and os.path.abspath(path) == want:
            return g
    return None


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
            vec = re.fullmatch(r"\{x:\s*([^,}]+),\s*y:\s*([^,}]+)\}", raw)
            if vec:
                v["vec2_fields"][key] = (float(vec.group(1)),
                                         float(vec.group(2)))
            st = _yaml_scalar_text(raw)
            if st is not None:
                v["str_fields"][key] = st
        inner = _embedded_values(sub)
        if inner:
            v["struct_values"] = inner
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


def _load_prefab_objects_for_types(root, type_names, guids, assets,
                                   typename_map, scenes=()):
    """Parse .prefab assets that author *type_names* MonoBehaviours.

    A prefab a packed scene places (*scenes*, in build order) yields one copy
    of its objects per PrefabInstance, tagged with that scene; one no scene
    places is parsed once as it is."""
    if not type_names:
        return []
    want_guids = set()
    for t in type_names:
        sp = typename_map.get(t)
        if not sp:
            continue
        g = _script_guid_for_path(guids, sp)
        if g:
            want_guids.add(g.lower())
    if not want_guids:
        return []
    placed = {}
    for si, spath in enumerate(scenes):
        stext = _scene_local_file_ids(_read(spath), si)
        for inst in _scene_prefab_instances(stext):
            ppath = (assets or {}).get(inst["prefab_guid"])
            if ppath:
                placed.setdefault(os.path.realpath(ppath), []).append(
                    (si, inst))
    out = []
    prefabs = list(_walk_files(root, (".prefab",)))
    for pi, path in enumerate(prefabs):
        raw = _read(path)
        low = raw.lower()
        if not any(g in low for g in want_guids):
            continue
        if prefabs and ((pi + 1) % 25 == 0 or pi + 1 == len(prefabs)):
            _progress("  prefab %d/%d %s" % (
                pi + 1, len(prefabs), os.path.basename(path)))
        copies = [(si, _prefab_instance_text(raw, inst))
                  for si, inst in placed.get(os.path.realpath(path), [])]
        pguid = next((g for g, ap in (assets or {}).items()
                      if os.path.realpath(ap) == os.path.realpath(path)), None)
        for si, text in copies or [(None, raw)]:
            objs, _l, _c, _h = parse_unity_yaml(
                text, guid_to_script=guids, asset_guids=assets)
            for o in objs:
                if o.get("class") in type_names:
                    if si is not None:
                        o["scene"] = si
                    else:
                        # an asset Instantiate copies, not a live object
                        o["prefab_asset"] = True
                        if pguid:
                            o["mb_ids"] = list(o.get("mb_ids") or []) + [
                                "%s@%s" % (mb, pguid)
                                for mb in o.get("mb_ids") or []]
                    out.append(o)
    return out


def _scene_list(root, paths):
    """Build scenes in buildIndex order: ``{name, path}`` (Unity ``Scene.name``
    is the file name without ``.unity``; ``Scene.path`` is project-relative)."""
    return [{
        "name": os.path.splitext(os.path.basename(p))[0],
        "path": os.path.relpath(p, root).replace(os.sep, "/"),
    } for p in paths]


def _load_scenes_lights_cameras(root, assets):
    """Parse scenes / tscn / blender JSON → objects, lights, cameras, hierarchy.

    Does not analyze scripts or load prefab extras. Sprite pixels are attached.
    A Godot project's scenes come from tools/godot_pack.py.
    """
    _godot.GODOT_ROOT[0] = None
    if _godot.is_godot_project(root):
        cameras = []
        objects, scene_names = _godot.load_godot_scenes(root, cameras)
        _load_scenes_lights_cameras.scenes = scene_names
        _load_scenes_lights_cameras.ui_layout = _godot.godot_screen_size(root)
        _progress("scene objects=%d cameras=%d" % (len(objects), len(cameras)))
        return objects, [], cameras, []
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
            "(looked for .unity / blender_pack.json, or a Godot project.godot / .tscn)" % root)
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


_MSCRIPT_GUID_RE = re.compile(
    r"m_Script:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-fA-F]+)")
_SOURCE_PREFAB_GUID_RE = re.compile(
    r"m_SourcePrefab:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-fA-F]+)")


def _add_mscript_paths_from_text(text, root, guids, out):
    """Add Assets/ .cs paths for each ``m_Script`` guid in *text*."""
    for m in _MSCRIPT_GUID_RE.finditer(text):
        sp = guids.get(m.group(1).lower())
        if sp and _is_player_csharp(root, sp):
            out.add(os.path.abspath(sp))


def _scripts_referenced_in_startup_scenes(root, assets, guids):
    """Project scripts authored on startup scenes + their source prefabs.

    Packed UI / stripped PrefabInstances often leave ``object["script"]``
    unset (scaffold Image/Button keep the GO, project MBs do not join).
    Falling back to every Assets ``.cs`` then full-analyzes vendor code
    (Destructible2D ``Stack<T>``, …) and refuses the pack. Scan YAML
    ``m_Script`` / ``m_SourcePrefab`` guids instead.
    """
    out = set()
    prefab_guids = set()
    for path in _unity_scenes_to_pack(root, asset_guids=assets):
        text = _read(path)
        _add_mscript_paths_from_text(text, root, guids, out)
        for m in _SOURCE_PREFAB_GUID_RE.finditer(text):
            prefab_guids.add(m.group(1).lower())
    for g in prefab_guids:
        ppath = (assets or {}).get(g)
        if not ppath or not ppath.lower().endswith(".prefab"):
            continue
        if not os.path.isfile(ppath):
            continue
        _add_mscript_paths_from_text(_read(ppath), root, guids, out)
    return out


def _analyze_scripts_and_prefabs(root, objects, assets):
    """Analyze scene scripts; pull missing MB types from prefabs.

    *objects* is extended in place with prefab instances. Returns analyses.
    """
    import tools.unity.unity_pack_common as _common
    _common.SHALLOW_METHODS.clear()
    if _godot.is_godot_project(root):
        return _godot.analyze_scripts(root, objects, analyze_script)
    _godot.GODOT_ROOT[0] = None
    guids = _guid_map(root, asset_guids=assets)
    typename_map = _mb_typename_to_script(root, guids)
    scene_scripts = set()
    for o in objects:
        sp = o.get("script")
        if sp:
            scene_scripts.add(os.path.abspath(sp))
    # Prefer authored scene / prefab m_Script guids when GO join missed them.
    scene_scripts |= _scripts_referenced_in_startup_scenes(
        root, assets, guids)
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

    needed = set()
    for a in analyses:
        needed |= set(a.get("getcomponent_types") or [])
        needed |= set(a.get("getcomponentsinchildren_types") or [])
        needed |= set(a.get("addcomponent_types") or [])
        needed |= set(a.get("findobject_types") or [])
        needed |= set(a.get("singleton_instance_types") or [])
        for c in a.get("classes") or []:
            for f in c.get("fields") or []:
                ty = f.get("ty") or ""
                if ty in typename_map:
                    needed.add(ty)
            for b in c.get("bases") or []:
                if b in ("MonoBehaviour", "ScriptableObject", "object",
                         "Object", "System"):
                    continue
                if b in typename_map:
                    needed.add(b)
    # Button onClick → project MB (e.g. BeginGame on a stripped GameManager).
    needed |= _onclick_mb_types(objects)
    have_classes = {o.get("class") for o in objects}
    missing = sorted(
        t for t in needed
        if t not in have_classes
        and t not in _ADDABLE_BUILTINS
        and t not in _PHYSICS_COMPONENTS
        and t not in _UI_GETCOMPONENT_TYPES
        and t not in _TRANSFORM_GETCOMPONENT_TYPES
        and t in typename_map)
    if missing:
        _progress("loading prefab components for %s" % ", ".join(missing))
        prefab_objs = _load_prefab_objects_for_types(
            root, set(missing), guids, assets, typename_map,
            scenes=_unity_scenes_to_pack(root, asset_guids=assets))
        objects.extend(prefab_objs)
        _attach_sprite_textures(prefab_objs, assets)
        # a prefab a script Instantiates runs: its clones need its methods
        spawned = set()
        for a in analyses:
            for c in a.get("classes") or []:
                spawned |= set(_prefab_instantiate_fields(
                    c, {"classes": dict.fromkeys(missing)}).values())
        for t in missing:
            sp = typename_map[t]
            if any(os.path.abspath(a.get("path") or "") == sp for a in analyses):
                continue
            a = analyze_script(sp)
            if t in spawned or not _derives_engine_type(a, typename_map):
                # a plain [Serializable] value (no component): its methods
                # are what the objects embedding it call
                analyses.append(a)
                continue
            full = {c["name"]: {m["name"] for m in c.get("methods") or []}
                    for c in a.get("classes") or []}
            a = analyze_script(sp, shallow=True)
            a["shallow"] = True
            for c in a.get("classes") or []:
                c["methods"] = []
                _common.SHALLOW_METHODS[c["name"]] = full.get(c["name"], set())
            a["apis"] = set()
            a["getcomponent_types"] = set()
            a["getcomponentsinchildren_types"] = set()
            a["addcomponent_types"] = set()
            analyses.append(a)
    analyses.extend(_analyze_static_refs(analyses, typename_map))

    analyses.extend(_inherit_base_members(analyses, typename_map))
    analyses.extend(_analyze_base_interfaces(root, guids, analyses))
    for a in analyses:
        for c in a.get("classes") or []:
            c["engine_derived"] = _derives_engine_type({"classes": [c]}, typename_map)

    # Scene stripped MB fileIDs (Button onClick targets) → pack instance mb_ids.
    _alias_onclick_mb_file_ids(objects)

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
    # a project class with no rows (nothing packs its methods; a base class
    # is analyzed only for its subclasses): its method names, for the trap
    rows = {o["class"] for o in objects}
    for t, sp in sorted(typename_map.items()):
        if t in rows or t in _common.SHALLOW_METHODS:
            continue
        try:
            with open(sp, encoding="utf-8", errors="replace") as f:
                src = cs2cpp._blank(f.read())
        except OSError:
            continue
        # no base: a plain value, its methods lowered where it is embedded
        if not re.search(r"\bclass\s+%s\b\s*(?:<[^>{]*>)?\s*:" % re.escape(t),
                         src):
            continue
        _common.SHALLOW_METHODS[t] = {
            n for n in re.findall(r"[\w>\]]\s+(\w+)\s*\([^;{}]*\)\s*\{", src)
            if n not in ("if", "while", "for", "foreach", "switch", "catch",
                         "using", "lock", "fixed")}
    return analyses


def load_project(root, asset_guids=None):
    """Load authored scenes + analyze scripts.

    *asset_guids* reuses a prior ``_asset_guid_map`` (avoids a second PackageCache
    walk). The map used is also stored on ``load_project.asset_guids``.
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise PackError("not a directory: %s" % root)
    _progress("scanning %s" % root)
    _progress("reading .meta guid maps")
    assets = asset_guids if asset_guids is not None else _asset_guid_map(root)
    load_project.asset_guids = assets
    objects, lights, cameras, hierarchy = _load_scenes_lights_cameras(
        root, assets)
    # ponytail: Tilemap tiles are drawn by pack() only
    objects = [o for o in objects if not o.get("tile")]
    analyses = _analyze_scripts_and_prefabs(root, objects, assets)
    return objects, analyses, lights, cameras, hierarchy


def _handle_streams(plan):
    """The handle fields, laid out for a GPU buffer of 32-bit words.

    A handle is an index into another class's instances (`idx:` member),
    at that class's width: 8 bits under `[MaxInstances(<=255)]`, 16 under
    65535, else 32. Each field is one stream of its class's capacity,
    packed from bit 0 -- four bytes, two shorts or one word per `uint` --
    and starting on a fresh word, so the shader finds entry `i` at word
    `off + i / per`, bits `(i % per) * bits`. Slots past the live count hold
    the null sentinel.
    """
    out, off = [], 0
    for cname, cl in sorted(plan["classes"].items()):
        cap = max(1, int(cl["n"]) + _mb_pool_extra(plan, cname))
        for name, _ty, bits, kind in cl["members"]:
            if not str(kind).startswith("idx:"):
                continue
            bits = int(bits)
            per = 32 // bits
            words = (cap + per - 1) // per
            out.append({"cls": cname, "idn": _c_ident(cname), "field": name,
                        "target": kind.split(":", 1)[1], "bits": bits,
                        "per": per, "cap": cap, "off": off, "words": words,
                        "null": _idx_null(bits)})
            off += words
    return out, off


def emit_handles_h(plan):
    """engine_handles.h: the stream layout, for the host that uploads it."""
    streams, total = _handle_streams(plan)
    lines = [
        "/* generated by tools/unity/unity_pack.py -- packed handles for the GPU */",
        "/* Upload engine_upload_handles() into a GLES 3.1 SSBO at binding 1;",
        "   shaders/handles.glsl reads it. OFF and WORDS count 32-bit words. */",
        "#ifndef ENGINE_HANDLES_H",
        "#define ENGINE_HANDLES_H",
        "#include <stdint.h>",
        "",
        "#define ENGINE_HANDLE_WORDS %d" % max(1, total),
    ]
    for st in streams:
        pre = "%s_%s" % (st["idn"], st["field"])
        lines.append("/* %s.%s -> %s: %d-bit, %d per word */"
                     % (st["cls"], st["field"], st["target"], st["bits"],
                        st["per"]))
        lines.append("#define %s_OFF %d" % (pre, st["off"]))
        lines.append("#define %s_LEN %d" % (pre, st["cap"]))
        lines.append("#define %s_BITS %d" % (pre, st["bits"]))
        lines.append("#define %s_NULL %du" % (pre, st["null"]))
    lines += ["",
              "int engine_handle_words(void); /* ENGINE_HANDLE_WORDS */",
              "int engine_upload_handles(uint32_t *dst, int max_words);",
              "",
              "#endif"]
    return "\n".join(lines) + "\n"


def emit_handles_c(plan):
    """engine_upload_handles: pack every handle stream into 32-bit words."""
    streams, total = _handle_streams(plan)
    total = max(1, total)
    lines = []
    p = lines.append
    p("")
    p("/* Packed handles for a GLES 3.1 SSBO (engine_handles.h,")
    p("   shaders/handles.glsl): each stream packed from bit 0, a slot past")
    p("   the live count holding the field's null. */")
    p("int engine_handle_words(void) { return %d; }" % total)
    p("")
    p("int engine_upload_handles(uint32_t *dst, int max_words) {")
    p("    int w;")
    p("    int k;")
    p("    if (!dst || max_words < %d) return 0;" % total)
    p("    for (w = 0; w < %d; w = w + 1) dst[w] = 0u;" % total)
    for st in streams:
        p("    /* %s.%s: %d-bit, %d per word, words [%d, %d) */"
          % (st["cls"], st["field"], st["bits"], st["per"], st["off"],
             st["off"] + st["words"]))
        p("    for (k = 0; k < %d; k = k + 1) {" % st["cap"])
        p("        uint32_t v = k < _%s_inst_count ? (uint32_t)%s_AT(k).%s : %du;"
          % (st["idn"], st["idn"], st["field"], st["null"]))
        if st["per"] == 1:
            p("        dst[%d + k] = v;" % st["off"])
        else:
            p("        dst[%d + k / %d] = dst[%d + k / %d] | (v << ((k %% %d) * %d));"
              % (st["off"], st["per"], st["off"], st["per"], st["per"],
                 st["bits"]))
        p("    }")
    p("    return %d;" % total)
    p("}")
    return "\n".join(lines) + "\n"


def emit_handles_glsl(plan):
    """shaders/handles.glsl: the SSBO and one accessor per handle stream.

    For inclusion after the shader's own `#version 310 es` (or later): GLES
    3.1 is what has SSBOs and `bitfieldExtract`."""
    streams, total = _handle_streams(plan)
    lines = [
        "/* generated by tools/unity/unity_pack.py -- packed handles (GLES 3.1+) */",
        "/* Include after `#version 310 es`. The host uploads",
        "   engine_upload_handles() (%d words) into binding 1. Each accessor"
        % max(1, total),
        "   returns an index into the target class's instances, or its",
        "   _NULL for none. */",
        "layout(std430, binding = 1) readonly buffer HandleSSBO {",
        "    uint handles[];",
        "};",
        "",
    ]
    for st in streams:
        pre = "%s_%s" % (st["idn"], st["field"])
        lines.append("/* %s.%s -> %s, %d-bit */" % (st["cls"], st["field"],
                                                     st["target"], st["bits"]))
        lines.append("const uint %s_NULL = %du;" % (pre, st["null"]))
        if st["per"] == 1:
            body = "handles[%du + i]" % st["off"]
        else:
            body = ("bitfieldExtract(handles[%du + i / %du], "
                    "int((i %% %du) * %du), %d)"
                    % (st["off"], st["per"], st["per"], st["bits"], st["bits"]))
        lines.append("uint %s(uint i) { return %s; }" % (pre, body))
        lines.append("")
    return "\n".join(lines) + "\n"


def emit_soa_positions_glsl(plan):
    """GLSL ES stub: std430 SSBO matching SoA tables (ES 3.1+ / desktop).

    GLES2 hosts keep using VBOs from engine_upload_positions; this file is
    the documented target for a later SSBO path and for --soa-vec4 / std140
    vec4 uploads.
    """
    lines = []
    p = lines.append
    p("/* generated by tools/unity/unity_pack.py — SoA position buffer layout */")
    p("/* OpenGL / GLSL target. Requires SSBO (std430) or upload as vec4[]. */")
    p("#version 310 es")
    p("precision highp float;")
    p("precision highp int;")
    p("")
    if not plan.get("soa"):
        p("/* Pack was AoS (--aos) — no SoA tables. Use engine_upload_positions gather. */")
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


# --hybrid: a method whose lowered C does not compile (crust refuses it at a `unity_pack:site` marker) is retried as a stub, which the
# hybrid pass then tries to run as managed code.  {(site path, C# line of the body): crust's message}; `pack` fills it between rounds.
_FORCE_MANAGED = {}
_LAST_CRUST_SITE = [None]


def validate_emitted_c(text, path="engine.c", analyses=None):
    """Gate generated C through cpprust, then compile the result with crust.

    unity_pack lowers by hand; this proves the result still sits inside the
    crust subset that `tools/cpprust.py` accepts — `_check_unsupported` plus
    a full `translate` pass (the csrust C++ half). The translated C is then
    compiled with `shivyc` so pack fails if crust cannot build it. Raises
    PackError on subset violations or crust compile failure (Unity-style site
    when a ``unity_pack:site`` marker, field accessor, or subset type error
    maps to authored C#).

    Large ``data.c`` (texture byte arrays) skips full validate — hundreds of
    MB through cpprust/crust is pathological; layout risk is covered by
    ``engine.c``.
    """
    if (path.startswith("data")
            and text is not None
            and len(text) > 8 * 1024 * 1024):
        _progress("skipping full validate for large %s (%d bytes)"
                  % (path, len(text)))
        return text
    import tools.cpprust as cpprust
    # Texture bytes are numbers in a brace list: the subset question is the
    # declaration, not 10^6 literals (most of a pack's validate time).
    blobs = {}

    def _elide(m):
        blobs[m.group(2)] = m.group(4)
        return "%s%s%s{ 0 }" % (m.group(1), m.group(2), m.group(3))
    text = _BYTE_BLOB_RE.sub(_elide, text)
    try:
        scan = cpprust._blank_directives(cpprust._strip_comments(text))
        cpprust._check_unsupported(scan, path)
        translated = cpprust.translate(text, path=path)
    except cpprust.CppError as e:
        raise PackError(_emitted_subset_error_to_unity(
            getattr(e, "message", None) or str(e),
            emitted_path=path,
            analyses=analyses,
            source_text=text))
    _crust_compile_c(translated, path, analyses=analyses, source_text=translated)
    for name, body in blobs.items():
        translated, n = re.subn(
            r"(unsigned char %s\[\d+\]\s*=\s*)\{ 0 \}" % re.escape(name),
            lambda m, body=body: m.group(1) + body, translated, count=1)
        if n != 1:
            raise PackError("%s: byte array %s lost in translation" % (path, name))
    return translated


#: `const unsigned char name[N] = { ... };` with a large literal body.
_BYTE_BLOB_RE = re.compile(
    r"(\bconst unsigned char )(\w+)(\[\d+\]\s*=\s*)(\{[\d\s,xXa-fA-F]{4096,}\})")


def _strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s or "")


def _crust_error_to_unity(err, source_text=None, analyses=None):
    """Map shivyc/clang diagnostics to Unity ``Assets/…(line,col): error …``."""
    err = _strip_ansi(err)
    # Undeclared Class_set_field / Class_get_field → field token in C#.
    m = re.search(r"undeclared identifier ['\"](\w+)['\"]", err)
    if m and analyses:
        ident = m.group(1)
        am = re.match(r"^(\w+)_(set|get)_(\w+)$", ident)
        if am:
            cls_idn, _op, field = am.group(1), am.group(2), am.group(3)
            site = _csharp_field_site(analyses, cls_idn, field)
            if site:
                path, text, idx = site
                return _cs_diag(
                    path, text, idx, "CS0103",
                    "The name '%s' does not exist in the current context"
                    % field)
    # tu.c:LINE:COL: error: … → nearest unity_pack:site marker above LINE.
    lm = re.search(
        r"(?:^|\n)(?:.*?[/\\])?tu\.c:(\d+)(?::(\d+))?:\s*error:\s*(.+)",
        err)
    if lm and source_text:
        line_no = int(lm.group(1))
        msg = lm.group(3).strip()
        lines = source_text.split("\n")
        site_path, site_line = None, 1
        for i in range(min(line_no, len(lines)) - 1, -1, -1):
            sm = re.match(
                r"\s*/\*\s*unity_pack:site\s+(\S+):(\d+)\s*\*/",
                lines[i])
            if sm:
                site_path, site_line = sm.group(1), int(sm.group(2))
                break
        if site_path:
            _LAST_CRUST_SITE[0] = (site_path, site_line, msg)
            return "%s(%d,1): error CS0000: %s" % (
                _assets_rel_path(site_path), site_line, msg)
    # Last resort: still Unity-shaped, not a raw /tmp path dump.
    first = err.split("\n")[0].strip() if err else "crust compile failed"
    first = re.sub(r"^.*?tu\.c:\d+(?::\d+)?:\s*", "", first)
    first = re.sub(r"^error:\s*", "", first)
    return "<generated>(1,1): error CS0000: %s" % (first or "crust compile failed")


def _csharp_field_site(analyses, class_idn, field):
    """(path, text, idx) of field name in class *class_idn*, or None."""
    for a in analyses or []:
        path = a.get("path") or ""
        text = None
        owner = None
        for c in a.get("classes") or []:
            cname = c.get("name") or ""
            if _c_ident(cname) != class_idn and cname != class_idn:
                continue
            owner = c
            if c.get("file_text") is not None:
                text = c["file_text"]
            break
        if owner is None:
            continue
        if text is None and path and os.path.isfile(path):
            text = _read(path)
        if not text:
            continue
        scan = cs2cpp._blank(text)
        # Prefer assignment / compound assign of the field.
        m = re.search(
            r"(?<![_\w])(%s)\s*(?:\+=|-=|\*=|/=|=(?!=))" % re.escape(field),
            scan)
        if not m:
            m = re.search(r"(?<![_\w])(%s)(?![\w])" % re.escape(field), scan)
        if m:
            return path, text, m.start(1)
    return None


def _csharp_type_site(analyses, tname):
    """(path, text, idx) of type token *tname* in authored C#, or None."""
    for a in analyses or []:
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
        for pat in (
            r"new\s+(%s)\s*(?:<|\()" % re.escape(tname),
            r"(?<![\w.])(%s)\s*<" % re.escape(tname),
            r"(?<![\w.])(%s)(?![\w])" % re.escape(tname),
        ):
            m = re.search(pat, scan)
            if m:
                return path, text, m.start(1)
    return None


_CS_KEYWORD_TYPES = frozenset((
    "string", "int", "uint", "long", "ulong", "short", "ushort", "byte",
    "sbyte", "float", "double", "decimal", "bool", "char", "object"))


def _csharp_new_array_site(analyses, tname):
    """(path, text, idx) of the first `new T[` in the scripts, or None."""
    pat = re.compile(r"(?<![\w.])new\s+(%s)\s*\[" % re.escape(tname))
    for a in analyses or []:
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
        m = pat.search(cs2cpp._blank(text))
        if m:
            return path, text, m.start()
    return None


def _emitted_subset_error_to_unity(err, emitted_path="engine.cpp",
                                   analyses=None, source_text=None):
    """Map cpprust subset failures to Unity ``Assets/…(line,col): error …``."""
    err = _strip_ansi(str(err or "")).strip()
    # engine.cpp: `new List` is not in the C++ subset …
    m = re.search(
        r"`new\s+(\w+)` is not in the C\+\+ subset", err)
    if m and m.group(1) in _CS_KEYWORD_TYPES:
        # `new int` refused by the subset is an *array creation* nothing
        # lowered (`new int[] { 1, 2 }`), not an unknown type: CS0246 here
        # said `int` could not be found, at the first `int` in any script.
        site = _csharp_new_array_site(analyses, m.group(1))
        msg = ("`new %s[..]` is not lowered yet: an array created in an "
               "expression" % m.group(1))
        if site:
            path, text, idx = site
            return _cs_diag(path, text, idx, "CS8000", msg)
        return "<cs>(1,1): error CS8000: %s" % msg
    if m:
        tname = m.group(1)
        site = _csharp_type_site(analyses, tname)
        if site:
            path, text, idx = site
            return _cs_diag(path, text, idx, "CS0246", _CS0246 % tname)
        return "<cs>(1,1): error CS0246: %s" % (_CS0246 % tname)
    # Prefer site markers / Unity shape from the crust mapper.
    clean = re.sub(
        r"^(?:.*[/\\])?%s(?::\d+(?::\d+)?)?:\s*" % re.escape(
            os.path.basename(emitted_path or "engine.cpp")),
        "", err, count=1)
    mapped = _crust_error_to_unity(
        clean or err, source_text=source_text, analyses=analyses)
    # Never surface the old packer prose.
    if "left the crust" in mapped or "cpprust subset" in mapped:
        first = (clean or err).split("\n")[0].strip() or "pack subset error"
        return "<generated>(1,1): error CS0000: %s" % first
    return mapped


def _crust_compile_c(text, path, defines=None, analyses=None, source_text=None):
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
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=repo)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise PackError(_crust_error_to_unity(
                err or ("exit %d" % r.returncode),
                source_text=source_text or text,
                analyses=analyses))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _refused_api_site(analyses, api):
    """First (path, text, idx) for a refused API token, or None."""
    patterns = {
        "Canvas": (
            r"AddComponent\s*<\s*(?:UnityEngine\.)?(Canvas)\s*>|"
            r"typeof\s*\(\s*(Canvas)\s*\)"
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


# ---------------------------------------------------------------------------
# Incremental pack: input fingerprint + per-output transpile skip
# ---------------------------------------------------------------------------

_STAMP_NAME = ".unity_pack_stamp.json"
_STAMP_VERSION = 4
_SCENE_CACHE_NAME = ".unity_pack_scene_cache"
_SCENE_CACHE_VERSION = 20
# Authored inputs under Assets/ that affect emit (skip Library / PackageCache).
_FINGERPRINT_EXTS = (
    ".cs", ".unity", ".prefab", ".meta",
    ".png", ".jpg", ".jpeg", ".tga", ".psd",
    ".wav", ".mp3", ".ogg",
    ".asset", ".controller", ".anim",
    ".ttf", ".otf", ".fontsettings",
    ".mat", ".physicMaterial", ".physicsMaterial2D",
    ".shader", ".cginc", ".hlsl",
    ".mixer",
)
_PACK_OUTPUTS = (
    "engine.c", "data.c", "main.c",
    "engine.cpp", "data.cpp", "main.cpp",
    "engine_draw.h",
)


def _sha256_text(s):
    """SHA-256 hex digest of a unicode string (UTF-8)."""
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _file_fingerprint_entry(path, root):
    """(relpath, size, mtime_ns) for one file; None if unreadable."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    rel = os.path.relpath(path, root).replace("\\", "/")
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
    return (rel, int(st.st_size), int(mtime_ns))


def _fingerprint_entries(root, box2d_root=None, coost_root=None):
    """Sorted (relpath, size, mtime_ns) for packer + project inputs.

    Every module that shapes the output counts, the Box2D-Packed glue
    generator included: an edit to any of them is a new pack.
    """
    root = os.path.abspath(root)
    entries = []
    unity_dir = os.path.dirname(os.path.abspath(__file__))
    tools_dir = os.path.dirname(unity_dir)
    names = sorted("unity/" + n for n in os.listdir(unity_dir)
                   if n.startswith("unity_pack") and n.endswith(".py"))
    for name in names + ["cs2cpp.py", "cpprust.py"]:
        e = _file_fingerprint_entry(os.path.join(tools_dir, name), tools_dir)
        if e:
            entries.append(("tools/%s" % name,) + e[1:])
    b2d = find_box2d_root(box2d_root)
    if b2d:
        e = _file_fingerprint_entry(os.path.join(b2d, "box2d_unity.py"), b2d)
        if e:
            entries.append(("box2d/box2d_unity.py",) + e[1:])
    coost = find_coost_root(coost_root)
    if coost:
        # What a string-using engine splices in: an edit is a new pack.
        for rel in ("include/co/fastring.h", "include/co/fast.h",
                    "include/co/mem.h", "include/co/def.h",
                    "include/co/__/dtoa_milo.h") + COOST_STRING_CORE:
            e = _file_fingerprint_entry(os.path.join(coost, rel), coost)
            if e:
                entries.append(("coost/" + rel,) + e[1:])
    ps = os.path.join(root, "ProjectSettings")
    if os.path.isdir(ps):
        for dirpath, _dns, names in os.walk(ps):
            for n in sorted(names):
                if n.startswith("."):
                    continue
                e = _file_fingerprint_entry(os.path.join(dirpath, n), root)
                if e:
                    entries.append(e)
    assets = os.path.join(root, "Assets")
    if os.path.isdir(assets):
        for path in _walk_files(assets, _FINGERPRINT_EXTS):
            e = _file_fingerprint_entry(path, root)
            if e:
                entries.append(e)
    elif _godot.is_godot_project(root):
        # No Assets/: without these a Godot project's fingerprint was empty,
        # and an edited scene or script was taken for "unchanged".
        gp = os.path.join(tools_dir, "godot_pack.py")
        e = _file_fingerprint_entry(gp, os.path.dirname(tools_dir))
        if e:
            entries.append(e)
        for path in _godot.godot_fingerprint_paths(root):
            e = _file_fingerprint_entry(path, root)
            if e:
                entries.append(e)
    entries.sort()
    return entries


def _hash_fingerprint_entries(entries, soa=True, soa_vec4=False,
                              gpu_handles=False):
    h = hashlib.sha256()
    h.update(b"soa=%d\n" % (1 if soa else 0))
    h.update(b"soa_vec4=%d\n" % (1 if soa_vec4 else 0))
    if gpu_handles:
        # Only when set: every fingerprint without it stays what it was.
        h.update(b"gpu_handles=1\n")
    for rel, size, mtime_ns in entries:
        h.update(("%s\0%d\0%d\n" % (rel, size, mtime_ns)).encode("utf-8"))
    return h.hexdigest()


def _input_fingerprints(root, soa=True, soa_vec4=False, gpu_handles=False,
                        box2d_root=None, coost_root=None):
    """(full, assets, scripts) fingerprints.

    *assets* covers tools, ProjectSettings, and non-``.cs`` Assets inputs.
    *scripts* covers ``Assets/**/*.cs`` only. *full* is the early-exit key.
    """
    entries = _fingerprint_entries(root, box2d_root, coost_root)
    script_entries = [e for e in entries if e[0].endswith(".cs")]
    asset_entries = [e for e in entries if not e[0].endswith(".cs")]
    full = _hash_fingerprint_entries(entries, soa=soa, soa_vec4=soa_vec4,
                                     gpu_handles=gpu_handles)
    assets = _hash_fingerprint_entries(
        asset_entries, soa=soa, soa_vec4=soa_vec4)
    scripts = _hash_fingerprint_entries(script_entries, soa=False, soa_vec4=False)
    return full, assets, scripts


def _input_fingerprint(root, soa=True, soa_vec4=False):
    """Cheap fingerprint of packer + project inputs (not PackageCache)."""
    return _input_fingerprints(root, soa=soa, soa_vec4=soa_vec4)[0]


def _scene_cache_path(outdir):
    return os.path.join(outdir, _SCENE_CACHE_NAME)


def _write_scene_cache(outdir, assets_fp, objects, lights, cameras, hierarchy,
                       asset_guids):
    """Persist scene graph for scripts-only incremental packs."""
    # Drop reloadable PNG pixels so deepcopy stays small; keep baked UI /
    # builtin rgba (no sprite_guid path to reload from).
    saved = []
    for o in objects:
        sp = o.get("sprite")
        if not isinstance(sp, dict) or "tex_rgba" not in sp:
            continue
        if sp.get("builtin") or sp.get("source") in ("ui", "ui_tmp"):
            continue
        # Godot sprites are cropped at import and have no .meta guid.
        if sp.get("godot"):
            continue
        if not sp.get("sprite_guid"):
            continue
        saved.append((sp, sp.pop("tex_rgba")))
    try:
        objs = copy.deepcopy(objects)
    finally:
        for sp, rgba in saved:
            sp["tex_rgba"] = rgba
    payload = {
        "cache_version": _SCENE_CACHE_VERSION,
        "assets_fingerprint": assets_fp,
        "objects": objs,
        "lights": copy.deepcopy(lights),
        "cameras": copy.deepcopy(cameras),
        "hierarchy": copy.deepcopy(hierarchy),
        "asset_guids": dict(asset_guids),
        "ui_layout": list(
            getattr(_load_scenes_lights_cameras, "ui_layout", None) or []),
        "scenes": list(
            getattr(_load_scenes_lights_cameras, "scenes", None) or []),
    }
    path = _scene_cache_path(outdir)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def _read_scene_cache(outdir, assets_fp):
    """Return cached scene tuple or None when missing/stale/corrupt."""
    path = _scene_cache_path(outdir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            payload = pickle.load(f)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("cache_version") or 0) != _SCENE_CACHE_VERSION:
        return None
    if payload.get("assets_fingerprint") != assets_fp:
        return None
    objects = payload.get("objects")
    asset_guids = payload.get("asset_guids")
    if not isinstance(objects, list) or not isinstance(asset_guids, dict):
        return None
    ul = payload.get("ui_layout") or []
    if isinstance(ul, (list, tuple)) and len(ul) >= 2:
        _load_scenes_lights_cameras.ui_layout = (int(ul[0]), int(ul[1]))
    _load_scenes_lights_cameras.scenes = list(payload.get("scenes") or [])
    return (
        objects,
        list(payload.get("lights") or []),
        list(payload.get("cameras") or []),
        list(payload.get("hierarchy") or []),
        asset_guids,
    )


def _stamp_path(outdir):
    return os.path.join(outdir, _STAMP_NAME)


def _read_stamp(outdir):
    path = _stamp_path(outdir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            stamp = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(stamp, dict):
        return None
    if int(stamp.get("version") or 0) != _STAMP_VERSION:
        return None
    return stamp


def _write_stamp(outdir, stamp):
    path = _stamp_path(outdir)
    with open(path, "w") as f:
        json.dump(stamp, f, indent=2, sort_keys=True)
        f.write("\n")


def _outputs_complete(outdir):
    """True when required pack artifacts exist under *outdir*."""
    for name in _PACK_OUTPUTS:
        if not os.path.isfile(os.path.join(outdir, name)):
            return False
    return True


def _class_stamp_entries(plan):
    """Light per-class rows for the stamp (enough for CLI summary)."""
    rows = []
    for name, cl in sorted((plan.get("classes") or {}).items()):
        row = {
            "name": name,
            "n": int(cl.get("n") or 0),
            "size": int(cl.get("size") or 0),
            "idx_ty": cl.get("idx_ty") or "int",
        }
        if cl.get("soa_dims"):
            row["soa_dims"] = int(cl["soa_dims"])
            if cl.get("soa_logical"):
                row["soa_logical"] = int(cl["soa_logical"])
        rows.append(row)
    return rows


def _plan_from_stamp(stamp):
    """Minimal plan dict for CLI / early-exit from stamp class rows."""
    classes = {}
    for entry in stamp.get("classes") or []:
        if isinstance(entry, str):
            # v1 stamps stored bare names only.
            classes[entry] = {
                "n": 0, "size": 0, "idx_ty": "int",
            }
            continue
        name = entry.get("name")
        if not name:
            continue
        cl = {
            "n": int(entry.get("n") or 0),
            "size": int(entry.get("size") or 0),
            "idx_ty": entry.get("idx_ty") or "int",
        }
        if entry.get("soa_dims"):
            cl["soa_dims"] = int(entry["soa_dims"])
            if entry.get("soa_logical"):
                cl["soa_logical"] = int(entry["soa_logical"])
        classes[name] = cl
    return {
        "classes": classes,
        "product_name": stamp.get("product_name") or "Player",
        "two_d": bool(stamp.get("two_d")),
        "soa": bool(stamp.get("soa")),
        "soa_vec4": bool(stamp.get("soa_vec4")),
        "physics": stamp.get("physics") or "box2d",
    }


def _write_if_different(path, text):
    """Write *text* only when missing or content differs. Returns True if wrote."""
    text = text if text is not None else ""
    if os.path.isfile(path):
        try:
            with open(path) as f:
                old = f.read()
            if old == text:
                return False
        except OSError:
            pass
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return True


def _cpp_twin(c_text):
    """C emit → C++-subset twin header for cpprust (same as pack())."""
    head = "/* generated by tools/unity/unity_pack.py — C++ subset for cpprust */\n"
    if not c_text.startswith("/*"):
        return head + c_text
    # one copy: data.c is ~600 MB, and a split then a concat made two
    return c_text.replace(c_text[:c_text.index("\n") + 1], head, 1)


def _emit_artifact_unchanged(outdir, cpp_name, c_name, cpp_text, force):
    """True when on-disk twin matches *cpp_text* and lowered .c exists."""
    if force:
        return False
    cpp_path = os.path.join(outdir, cpp_name)
    c_path = os.path.join(outdir, c_name)
    if not os.path.isfile(cpp_path) or not os.path.isfile(c_path):
        return False
    try:
        with open(cpp_path) as f:
            old = f.read()
    except OSError:
        return False
    return old == cpp_text


def pack(root, outdir, *args, **kwargs):
    """Pack a Unity (or Godot) project. See `_pack_impl`.

    Static helper classes and extension methods are rewritten at the source
    level first (tools/unity/unity_pack_extensions.py): the analysis and the
    lowering read the rewritten text through `SOURCE_OVERLAY`, and every
    original line stays on its line, so diagnostics point at the author's.
    """
    import tools.unity.unity_pack_common as _common
    import tools.unity.unity_pack_extensions as _ext
    _common.GPU_BATCH[0] = bool(kwargs.pop("gpu_batch", False))
    files = {}
    import tools.unity.unity_pack_gpu2d as _gpu2d
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in (
            "Library", "Temp", "Logs", "obj", "Packages", ".godot", ".git")]
        for fn in fns:
            if fn.endswith(".cs"):
                fp = os.path.join(dp, fn)
                try:
                    with open(fp) as f:
                        files[fp] = f.read()
                except (OSError, UnicodeDecodeError):
                    pass
    _common.SOURCE_ORIGINAL.clear()
    _common.SOURCE_ORIGINAL.update({os.path.abspath(fp): txt for fp, txt in files.items()})
    # SpriteEffects2D.Set / Clear (the 2D path's effect byte): rewritten
    # before the static helpers are copied into their callers
    _fx_changed = []
    for fp in list(files):
        t2 = _gpu2d.desugar_effects(files[fp])
        if t2 != files[fp]:
            files[fp] = t2
            _fx_changed.append(fp)
    _common.FX_USED[0] = bool(_fx_changed)
    overlay = _ext.desugar_project(files) if files else {}
    # Stack / Queue / HashSet, as the List the packer lowers
    # (tools/unity/unity_pack_collections.py).
    import tools.unity.unity_pack_collections as _coll
    import tools.unity.unity_pack_physics as _phys
    _layer_names = _phys.read_layer_names(root)
    import tools.unity.unity_pack_input as _inp
    _common.SOURCE_INPUT_ACTIONS[:] = []
    _inp.HANDLERS[:] = []
    _inp.ROOTS.clear()
    _ia_serialized = _inp.serialized_actions(root)
    _common.SOURCE_LAYER_NAMES.clear()
    _common.SOURCE_LAYER_NAMES.update(_layer_names)
    _common.SOURCE_LAYER_MATRIX[:] = _phys.read_layer_matrix(root) or []
    _common.SOURCE_ENABLED_WRITTEN.clear()
    for t in files.values():
        scan = cs2cpp._blank(t)
        if re.search(r"(?<![\w.])(?:this\s*\.\s*)?enabled\s*=(?!=)", scan):
            _common.SOURCE_ENABLED_WRITTEN.update(
                re.findall(r"\bclass\s+(\w+)", scan))
        for m in re.finditer(r"(\w+)\s*(?:>\s*\(\s*\))?\s*\.\s*enabled\s*"
                             r"=(?!=)", scan):
            r = m.group(1)
            gc = re.search(r"GetComponent\w*\s*<\s*(\w+)\s*>\s*\(\s*\)\s*\."
                           r"\s*enabled\s*=(?!=)", scan[max(0, m.start() - 80):
                                                        m.end()])
            if gc:
                _common.SOURCE_ENABLED_WRITTEN.add(gc.group(1))
            _common.SOURCE_ENABLED_WRITTEN.update(re.findall(
                r"(\w+)\s*(?:\[\s*\])?\s+%s\b" % re.escape(r), scan))
    _has_remove = _phys.has_standard_remove(files.values())
    _items = _item_gain_types(files.values())
    _n = [0]
    for fp in sorted(files):
        t = _coll.desugar_null_conditional(overlay.get(fp, files[fp]))
        t = _coll.desugar_local_arrays(t, _n)
        t = _coll.desugar_list_initializers(t)
        t = _coll.desugar_collections(t, _n)
        t = _phys.desugar_layers(t, _layer_names,
                                 _common.SOURCE_LAYER_MATRIX, _has_remove)
        t = _inp.desugar_input_actions(t, _common.SOURCE_INPUT_ACTIONS,
                                       os.path.relpath(fp, root),
                                       _ia_serialized.get(os.path.abspath(fp)))
        t = _desugar_destroy_immediate(t)
        t = _desugar_drop_achievements_sounds(t, os.path.relpath(fp, root))
        t = _desugar_drop_item_gains(t, os.path.relpath(fp, root), _items)
        t = _coll.desugar_bytes(t)
        t = _coll.desugar_multidim(t)
        t = _coll.desugar_list_foreach(t, _n)
        t = _coll.desugar_string_switch(t)
        # Coroutines, as state machines (tools/unity/unity_pack_coroutines.py).
        import tools.unity.unity_pack_coroutines as _co
        t = _co.desugar_coroutines(t)
        if t != files[fp] or fp in _fx_changed:
            overlay[fp] = t
    # An API a copied or inlined static helper uses is its caller's too:
    # the per-script scan does not see it there.
    _common.SOURCE_API_HINTS.clear()
    if _common.SOURCE_INPUT_ACTIONS:
        _ia_code, _ia_keys, _ia_hints = _inp.engine_tables(_common.SOURCE_INPUT_ACTIONS)
        _common.SOURCE_API_HINTS.update(_ia_hints)
        for act in _common.SOURCE_INPUT_ACTIONS:
            if not act["bindings"]:
                sys.stderr.write(
                    "unity_pack: warning: %s: InputAction %r has no binding the pack "
                    "can read (a serialized one is not read): it is never pressed\n"
                    % (act["where"], act["name"]))
    if any(re.search(r"\.\s*active(?:Self|InHierarchy)\b", t)
           for t in files.values()):
        _common.SOURCE_API_HINTS.add("GameObject.SetActive")
    _STATIC_HELPERS.pop(root, None)
    saved = dict(_common.SOURCE_OVERLAY)
    for fp, text in overlay.items():
        _common.SOURCE_OVERLAY[fp] = text
        _common.SOURCE_OVERLAY[os.path.abspath(fp)] = text
    _FORCE_MANAGED.clear()
    try:
        for _round in range(40):
            _LAST_CRUST_SITE[0] = None
            try:
                return _pack_impl(root, outdir, *args, **kwargs)
            except PackError:
                site = _LAST_CRUST_SITE[0]
                if not (kwargs.get("hybrid") or kwargs.get("managed")) or site is None \
                        or (site[0], site[1]) in _FORCE_MANAGED:
                    raise
                # --hybrid: a method crust cannot compile is a method the lowering could not do (managed code, or a CS8000 stub)
                _FORCE_MANAGED[(site[0], site[1])] = site[2]
                _progress("hybrid: %s(%d): the lowered C does not compile (%s); trying managed code"
                          % (site[0], site[1], site[2]))
        return _pack_impl(root, outdir, *args, **kwargs)
    finally:
        _FORCE_MANAGED.clear()
        _common.SOURCE_OVERLAY.clear()
        _common.SOURCE_OVERLAY.update(saved)


def _pack_impl(root, outdir, soa=True, soa_vec4=False, force=False, strict=None,
         gpu_handles=False, physics_inject=False, box2d_root=None,
         coost_root=None, hybrid=False, managed=None, gpu_physics=None):
    """Pack the Unity-subset project at *root* into *outdir*.

    gpu_physics: run the 2D physics on the gpu-physics-engine circle solver
    instead of Box2D-Packed (True, or the checkout's path; also
    $GPU_PHYSICS_ROOT or ../gpu-physics-engine). See tools/unity/unity_pack_gpu_physics.py.

    managed: run whole script classes as managed C# on DotNetAnywhere instead
    of lowering them (implies hybrid): "*" for every script class, or a
    collection of class names. A class whose managed code cannot be built
    keeps its lowered C (tools/unity/unity_pack_hybrid.py).

    2D physics (Rigidbody2D, Collider2D) is Box2D-Packed: box2d_unity.py from
    the Box2D-Packed checkout at *box2d_root*, $BOX2D_PACKED_ROOT, or a
    ``box2d`` directory beside this repository. C# string locals are coost
    fastrings, spliced into engine.cpp when a script has one: the coost
    checkout at *coost_root*, $COOST_ROOT, or a ``coost`` directory beside
    this repository (only needed then). physics_inject=True routes
    contact begin / end through box2d_pack injection markers instead of
    Box2D's event arrays.
    """
    physics_key = "box2d+inject" if physics_inject else "box2d"
    if gpu_physics:
        import tools.unity.unity_pack_gpu_physics as _gpuphys
        physics_key = "gpu:" + _gpuphys.fingerprint(gpu_physics)
    if managed:
        hybrid = True
    if hybrid:
        force = True      # (the stamp does not know about --hybrid: a hybrid pack must not reuse a plain one, nor the reverse)
    _TYPE_DECL_ROOT[0] = os.path.abspath(root)
    os.makedirs(outdir, exist_ok=True)
    fp, assets_fp, scripts_fp = _input_fingerprints(
        root, soa=soa, soa_vec4=soa_vec4, gpu_handles=gpu_handles,
        box2d_root=box2d_root, coost_root=coost_root)
    if not force:
        stamp = _read_stamp(outdir)
        if (stamp
                and stamp.get("input_fingerprint") == fp
                and bool(stamp.get("soa")) == bool(soa)
                and bool(stamp.get("soa_vec4")) == bool(soa_vec4)
                and stamp.get("physics") == physics_key
                and _outputs_complete(outdir)):
            _progress("unchanged; skipping pack (stamp match)")
            return _plan_from_stamp(stamp)

    cached = None if force else _read_scene_cache(outdir, assets_fp)
    if cached is not None:
        _progress("assets unchanged; reusing scenes (scripts-only rebuild)")
        objects, lights, cameras, hierarchy, asset_guids = cached
        _attach_sprite_textures(objects, asset_guids)
        load_project.asset_guids = asset_guids
    else:
        _progress("scanning %s" % os.path.abspath(root))
        _progress("reading .meta guid maps")
        asset_guids = _asset_guid_map(root)
        load_project.asset_guids = asset_guids
        objects, lights, cameras, hierarchy = _load_scenes_lights_cameras(
            root, asset_guids)
        try:
            _write_scene_cache(
                outdir, assets_fp,
                objects, lights, cameras, hierarchy, asset_guids)
        except OSError:
            pass
    tiles = [o for o in objects if o.get("tile")]
    objects = [o for o in objects if not o.get("tile")]
    analyses = _analyze_scripts_and_prefabs(root, objects, asset_guids)
    used_apis = set()
    for a in analyses:
        used_apis |= a["apis"]
    # the button names scripts read (Input.GetButton / Down / Up)
    _bn = set()
    for a in analyses:
        for c in a.get("classes") or []:
            _bn |= set(re.findall(
                r'Input\s*\.\s*GetButton(?:Down|Up)?\s*\(\s*"([^"]+)"',
                c.get("file_text") or ""))
    if _bn:
        plan_buttons = sorted(_bn | {"Jump"})
    else:
        plan_buttons = None
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
    if plan_buttons:
        plan["input_buttons"] = plan_buttons
    if _godot.is_godot_project(root):
        plan["godot"] = _godot.physics_settings(root)
    else:
        plan["unity_physics"] = unity_physics_settings(root)
        plan["physics2d_queries_hit_triggers"] = plan["unity_physics"][
            "queries_hit_triggers"]
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
    gc_types -= set(_collect_interfaces(analyses))
    plan["getcomponent_types"] = sorted(gc_types)
    gcic_types = set()
    for a in analyses:
        gcic_types |= set(a.get("getcomponentsinchildren_types") or [])
    _validate_getcomponent_types(gcic_types, plan, analyses)
    plan["getcomponentsinchildren_types"] = sorted(gcic_types)
    plan["mb_bases"] = _collect_mb_bases(analyses)
    plan["empty_lists"] = _empty_instance_lists(plan)
    plan["interfaces"] = _collect_interfaces(analyses)
    plan["scene_manager"] = "SceneManager" in used_apis
    plan["static_getters"] = _collect_static_getters(analyses)
    plan["static_setters"] = _collect_static_setters(analyses)
    plan["player_prefs"] = _uses_player_prefs(analyses)
    plan["addcomponent_types"] = sorted(add_types)
    plan["addcomponent_budget"] = _addcomponent_budget(analyses, plan)
    # AddComponent<XJoint2D>(): the joint tables' spares, and the Rigidbody2D
    # Unity adds with a joint when the GameObject has none.
    _jadd = sum(int(v) for t, v in plan["addcomponent_budget"].items()
                if t in _JOINT2D_KINDS)
    # (the joint table is built later: the capacity is authored + these)
    plan["joints2d_add"] = _jadd
    if _jadd:
        plan["physics2d_joints"] = True
        plan["addcomponent_budget"]["Rigidbody2D"] = int(
            plan["addcomponent_budget"].get("Rigidbody2D") or 0) + _jadd
    plan["instantiate_budget"] = _instantiate_budget(analyses, plan)
    # a prefab Instantiate'd from a field reuses destroyed clones' slots;
    # ponytail: at most _PREFAB_SPAWN_POOL live clones, the next one stops
    # the program (`_rewrite_instantiate`) -- raise it, or [MaxInstances(N)]
    for a in analyses:
        for c in a.get("classes") or []:
            if c["name"] not in plan["classes"]:
                continue
            for _t in _prefab_instantiate_fields(c, plan).values():
                plan.setdefault("prefab_spawn", set()).add(_t)
                _tc = plan["classes"][_t]
                if _tc.get("max_instances") is None:
                    _tc["max_instances"] = int(_tc["n"]) + _PREFAB_SPAWN_POOL
            # ponytail: at most _GO_CLONE_POOL live clones of a child; the
            # next Instantiate stops the program -- raise it, or [MaxInstances]
            # (a child with no packed script stops it too: GameObject_Instantiate)
            if re.search(r"(?<![\w.])(?:(?:UnityEngine\.)?Object\.)?Instantiate\s*\(\s*"
                         r"(?:this\s*\.\s*)?\w+\s*\.\s*GetChild\s*\(", cs2cpp._blank(
                             "\n".join(m.get("body") or "" for m in c.get("methods") or []))):
                plan.setdefault("go_clone_classes", set())
            for _t in _child_clone_classes(c, plan):
                plan.setdefault("go_clone_classes", set()).add(_t)
                _tc = plan["classes"][_t]
                if _tc.get("max_instances") is None:
                    _tc["max_instances"] = int(_tc["n"]) + _GO_CLONE_POOL
    if plan.get("godot"):
        # Godot's PackedScene.Instantiate: a template root's class spawns,
        # [MaxInstances(N)] or a default number of spare rows
        plan["godot_spawn"] = _godot.spawn_classes()
        plan["godot_templates"] = _godot.spawn_templates()
        # connections within a spawned scene: their ends are known only
        # when it is spawned (a table per connection, by sender row)
        plan["godot_conns"] = [
            {"t": ti, "from": j, "to": c["to"], "signal": c["signal"],
             "method": c["method"], "from_class": nd["class"],
             "to_class": t["nodes"][c["to"]]["class"]}
            for ti, t in enumerate(plan["godot_templates"])
            for j, nd in enumerate(t["nodes"]) for c in nd.get("conns") or []]
        # a spawned body's class: its rows are the pool's dormant copies
        # (godot_pack), so a spawn takes a freed one -- never a spare row,
        # which would have no collider or Box2D body
        plan["godot_pooled"] = sorted(
            cname for cname, gcl in plan["classes"].items()
            if any(o.get("godot_pool") for o in gcl.get("instances") or []))
        for _pc in plan["godot_pooled"]:
            plan["classes"][_pc]["max_instances"] = int(
                plan["classes"][_pc]["n"])
            plan["instantiate_budget"][_pc] = max(
                1, int(plan["instantiate_budget"].get(_pc) or 0))
        for _sc in plan["godot_spawn"]:
            if _sc in plan["classes"]:
                plan["instantiate_budget"][_sc] = max(
                    int(plan["instantiate_budget"].get(_sc) or 0),
                    _godot.DEFAULT_SPAWN_BUDGET)
    plan["new_budget"] = _new_budget(analyses, plan)
    # Each clone takes a GameObject too: a `[MaxInstances(N)]` class's share
    # of the pool is what fills it to N, not the one spare per call site.
    plan["instantiate_go_budget"] = sum(
        (_mb_pool_extra(plan, cname)
         if (plan["classes"].get(cname) or {}).get("max_instances") is not None
         else int(v))
        for cname, v in (plan["instantiate_budget"] or {}).items())
    # `new GameObject(..)` takes a slot of the same table
    plan["newgo_budget"] = _newgo_budget(analyses, plan)
    plan["instantiate_go_budget"] += plan["newgo_budget"]
    plan["instantiate_types"] = sorted(plan["instantiate_budget"] or {})
    plan["disallow_multiple_types"] = sorted(_disallow_multiple_types(analyses))
    fot_types = set()
    sing_types = set()
    for a in analyses:
        fot_types |= set(a.get("findobject_types") or [])
        sing_types |= set(a.get("singleton_instance_types") or [])
    fot_types |= sing_types
    plan["findobject_types"] = sorted(fot_types)
    plan["singleton_instance_types"] = sorted(sing_types)
    plan["singleton_field_types"] = sorted(
        {t for a in analyses for t in (a.get("singleton_field_types") or ())})
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
    plan["textures"] = _collect_textures(objects + tiles)
    plan["tiles"] = [{k: v for k, v in o["sprite"].items() if k != "tex_rgba"}
                     for o in tiles
                     if o.get("sprite") and "tex_id" in o["sprite"]]
    import tools.unity.unity_pack_common as _cmn
    if _cmn.GPU_BATCH[0]:
        # the 2D GPU path's atlas (tools/unity/unity_pack_gpu2d.py) -- built once
        # every texture is in the table (see below)
        plan["_gpu_batch"] = True
    _ensure_texture_guids(
        plan["textures"], _anim_sprite_guids(objects)
        + _animator_sprite_guids(objects, asset_guids), asset_guids)
    plan["sprite_ref_tex"] = _sprite_ref_textures(plan["textures"], sorted({
        (o.get("object_refs") or {}).get(f["name"])
        for cl in plan["classes"].values() for f in cl.get("fields") or []
        if f.get("ty") == "Sprite" for o in cl.get("instances") or []}
        - {None}), asset_guids)
    # a MeshRenderer's material's _MainTex (tools/unity/unity_pack_mesh.py)
    for _o in objects:
        _md = _o.get("mesh_draw")
        if not _md or not _md.get("mat_guid"):
            continue
        _mp = asset_guids.get(_md["mat_guid"])
        _tg = _mesh.material_main_tex(_read(_mp)) if _mp and os.path.isfile(_mp) else None
        if _tg:
            _ensure_texture_guids(plan["textures"], [_tg], asset_guids)
            _md["tex_id"] = next((k for k, t in enumerate(plan["textures"])
                                  if t.get("guid") == _tg), -1)
    if plan.get("_gpu_batch"):
        # Sprite lights' cookies, in the texture table (so the atlas)
        _cookies = [L.get("cookie_guid") for o in objects
                    for L in (o.get("lights2d") or []) if L.get("cookie_guid")]
        if _cookies:
            plan["_cookie_tex"] = _ensure_texture_guids(
                plan["textures"], _cookies, asset_guids)
    kb_keys = set()
    for a in analyses:
        kb_keys |= set(a.get("keyboard_keys") or [])
    plan["keyboard_keys"] = sorted(kb_keys)
    plan["physics2d_contacts"] = _uses_collision2d_contacts(analyses)
    company, product = player_identity(root)
    plan["company_name"] = company
    plan["product_name"] = product
    plan["project_root"] = os.path.abspath(root)
    plan["data_path"] = os.path.join(os.path.abspath(root), "Assets")
    plan["persistent_data_path"] = unity_persistent_data_path(company, product)
    sw, sh, sfs, snative, smax = player_display(root)
    if _godot.is_godot_project(root):
        sw, sh = _godot.godot_window_size(root)
    plan["screen_width"] = sw
    plan["screen_height"] = sh
    plan["screen_fullscreen"] = sfs
    plan["screen_fullscreen_native"] = snative
    plan["screen_maximized"] = smax
    # CanvasScaler / viewSize letterbox units used by _bake_ui_images.
    # Live RT recompute must use the same space (not a resized Screen).
    ul = getattr(_load_scenes_lights_cameras, "ui_layout", None)
    if ul and len(ul) >= 2:
        ulw, ulh = int(ul[0]), int(ul[1])
    else:
        ulw, ulh = _ui_layout_screen(root, objects)
    plan["ui_layout_width"] = max(1, int(ulw))
    plan["ui_layout_height"] = max(1, int(ulh))
    _seed_camera_script_view(plan, objects)
    gview = (plan.get("camera") or {}).get("godot_view")
    if gview:
        # Godot's view: the viewport's size over the zoom, whatever the
        # window's shape (letterboxed, as stretch aspect "keep").
        plan["camera_aspect"] = gview["half"][0] / gview["half"][1]
        plan["camera_rect"] = (0.0, 0.0, 1.0, 1.0)
    go_names, go_comps = _build_go_tables(plan)
    if plan.get("godot"):
        plan["godot_signals"] = _godot.resolve_signals(plan)
        plan["godot_input"] = _godot.input_plan()
        plan["godot_custom"] = _godot.resolve_custom_signals(plan)
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
    plan["go_has_sprite"] = sorted(_gos_with_sprite(plan))
    plan["go_active"] = _build_go_active(plan, go_names)
    plan["go_tags"] = _build_go_tags(plan, go_names)
    plan["go_layers"] = _build_go_layers(plan, go_names)
    plan["scenes"] = list(
        getattr(_load_scenes_lights_cameras, "scenes", None) or [])
    plan["go_scene"] = _build_go_scene(plan, go_names)
    plan["go_components"] = go_comps
    plan["go_ui_components"] = _build_go_ui_component_maps(plan, analyses)
    plan["go_parents"] = _build_go_parents(plan)
    plan["go_siblings"] = _build_go_sibling_indices(plan["go_parents"])
    plan["ui_buttons"] = _build_ui_buttons(plan, analyses)
    plan["ui_sliders"] = _build_ui_sliders(plan, analyses)
    plan["ui_scrollbars"] = _build_ui_scrollbars(plan, analyses)
    plan["ui_scrollrects"] = _build_ui_scrollrects(plan, analyses)
    _link_scrollrects_scrollbars(plan)
    plan["ui_toggles"] = _build_ui_toggles(plan, analyses)
    plan["ui_eventtriggers"] = _build_ui_eventtriggers(plan, analyses)
    plan["live_rt"] = _build_rect_transforms(plan)
    rb2d, rb3d, go_rb2d, go_rb3d, rb2d_by_fid, rb3d_by_fid = (
        _build_rigidbody_tables(plan))
    plan["rigidbody2d"] = rb2d
    plan["rb2d_by_file_id"] = rb2d_by_fid
    _build_joint2d_table(plan)
    _parts.build_particle_table(plan)
    _curves.build_table(plan)
    _mesh.build_table(plan, objects)
    try:
        _lines.build_table(plan)
    except _lines.LineError as e:
        raise PackError(str(e))
    # Rigidbody2D rotation: a body Box2D-Packed may turn -- not static, not
    # FreezeRotation -- turns its owner's Transform, which then keeps a live
    # rotation (drawn rotated). Unity mode; the glue locked it before.
    rotating = [r for r in rb2d if not r.get("freeze_rot")
                and int(r.get("body_type") or 0) != 2
                and r.get("owner_class") in plan["classes"]
                and _class_has_position(plan["classes"][r["owner_class"]])]
    if rotating and not plan.get("godot"):
        plan["physics2d_rotation"] = True
        plan["live_rot_classes"] = sorted(
            set(plan.get("live_rot_classes") or [])
            | {r["owner_class"] for r in rotating})
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
    _resolve_go_field_refs(plan)
    _tmp_text_variants(plan, analyses)
    _attach_transform_parents(plan)
    if plan.get("godot_spawn"):
        # a clone is added under a node at runtime (AddChild)
        plan["has_transform_parents"] = True
    if "transform.SetParent" in used_apis:
        plan["has_transform_parents"] = True
    plan["collider2d"] = _build_collider2d_tables(plan)
    # Godot's collision layers: filtered in Box2D-Packed's Godot mode and
    # (areas) in the signal dispatch, when any is not layer 1 / mask 1.
    plan["physics2d_layers"] = bool(plan.get("godot")) and any(
        (c.get("godot_layer", 1), c.get("godot_mask", 1)) != (1, 1)
        for c in plan["collider2d"])
    plan["collider3d"] = _build_collider3d_tables(plan)
    plan["animation"] = _build_animation_tables(plan)
    plan["animators"] = _build_animator_tables(plan, asset_guids, {
        (cn, f["name"]): f.get("ty") for cn, c in plan["classes"].items()
        for f in c.get("fields") or []})
    # a clip's rotation / scale curves need their owners' live tables
    plan["live_rot_classes"] = sorted(set(plan.get("live_rot_classes") or [])
                                      | set(plan.get("anim_rot_classes") or []))
    _resolve_transform_field_targets(plan)
    # Transform.TransformPoint / matrices need live localScale (seeded authored).
    live_scale = set(plan.get("live_scale_classes") or [])
    live_scale |= set(plan.get("anim_scale_classes") or [])
    live_scale |= set(plan.get("transform_point_classes") or [])
    live_scale |= set(plan.get("transform_matrix_classes") or [])
    plan["live_scale_classes"] = sorted(live_scale)
    os.makedirs(outdir, exist_ok=True)
    _progress("emitting engine.c (%d classes)" % len(plan["classes"]))
    # A method the translator cannot lower is a warning, or with `strict`
    # an error (`_report_stub`).
    # A stub -- a method emptied because something in it is not lowered yet
    # -- is an error for a Godot project. GODOT_PACK.md promises that what is
    # not in the subset "is refused where it is written, never dropped", and
    # a stub is exactly a drop: a lambda, a tuple or a static call nothing
    # lowers emptied its method with only a CS8000 warning. A Unity project
    # keeps warnings by default until the move to cs2cpp is done (see
    # UNITY_PACK.md); either can be set explicitly.
    if strict is None:
        strict = _godot.is_godot_project(root)
    plan["strict"] = bool(strict)
    plan["hybrid"] = bool(hybrid)
    if isinstance(managed, str) and managed != "*":
        managed = [m for m in managed.split(",") if m]       # ("A,B", as on the command line)
    plan["managed"] = "*" if managed == "*" else (set(managed) if managed else set())
    import tools.unity.unity_pack_common as _common
    used_apis = set(used_apis) | _common.SOURCE_API_HINTS
    if plan.get("_gpu_batch"):
        import tools.unity.unity_pack_gpu2d as _gpu
        os.makedirs(outdir, exist_ok=True)
        plan["gpu_atlas"] = _gpu.build_atlas(plan.get("textures") or [], outdir, root)
        _gpu.build_lights(plan, _load_sorting_layers(root))
    engine = emit_engine(plan, analyses, used_apis)
    import tools.unity.unity_pack_hybrid as _hybrid
    if plan.get("hybrid"):
        # a method the lowering left a stub: managed code on DotNetAnywhere where that compiles (tools/unity/unity_pack_hybrid.py)
        plan["_root"] = root
        engine = _hybrid.apply(plan, engine, outdir, _report_stub, _progress)
    else:
        _hybrid.clean(outdir)
    _used_helpers = plan.pop("_cs_str_used", None) or set()
    _used_helpers = {h for h in _used_helpers if not re.search(
        r"^static [\w\s*]+\b%s\(" % re.escape(h), engine, re.M)}
    helpers_c = _string_helpers_c(_used_helpers)
    if (runtime.needs_math(runtime.closure(
            {h for h in _used_helpers if runtime.is_runtime_helper(h)}))
            or _LIBM_CALL.search(engine)) \
            and "#include <math.h>" not in engine:
        k = engine.index("#include <stdint.h>\n") + len("#include <stdint.h>\n")
        engine = engine[:k] + "#include <math.h>\n" + engine[k:]
    # only the types a call survived for: a body that recorded one and was
    # then stubbed (Slime Jump's `List<Vector3Int>`) may leave the type
    # itself undeclared
    vec_types = [t for t in (plan.pop("_vec_helper_types", None) or set())
                 | {"int"}
                 if re.search(r"\b_cs_vec_(?:index|remove|at|new)_%s\(" % re.escape(
                     _vec_helper_suffix(t)), engine)]
    if not vec_types and re.search(r"\b_cs_vec_check_(?:index|insert)\(", engine):
        # List.RemoveAt / Insert (a LinkedList's AddFirst / RemoveFirst too) call the
        # bounds checks, which the search helpers define; with no search in the program
        # nothing asked for them and the call was left undeclared
        vec_types = ["int"]
    if vec_types:
        helpers_c += _vec_helpers_c(vec_types)
    json_protos, json_defs = [], []
    for jc, what in sorted((plan.pop("_json_classes", None) or {}).items()):
        pr, df = _json_class_c(jc, plan["classes"][jc], what)
        json_protos.append(pr)
        json_defs.append(df)
    plan.pop("_json_string_fns", None)
    if json_protos:
        helpers_c += "\n".join(json_protos) + "\n"
        engine = engine.rstrip("\n") + "\n\n/* JsonUtility, per class */\n" \
            + "\n\n".join(json_defs) + "\n"
    if _CS_STRING_HELPER_MARKER in engine:
        engine = engine.replace(_CS_STRING_HELPER_MARKER + "\n", helpers_c, 1)
    if _CS_STRING_HELPER_PRELUDE in helpers_c \
            and "#include <stdio.h>" not in engine:
        # the prelude reports through stdio; the string block that used to
        # be its only home includes it, an engine without one does not
        k = engine.index("#include <stdint.h>\n") + len("#include <stdint.h>\n")
        engine = engine[:k] + "#include <stdio.h>\n" + engine[k:]
    # C# string locals are coost fastrings (`_own_string_locals`). Only an
    # engine that has one needs coost: splice its string core in after the
    # C headers, for cpprust to lower with the rest.
    if "std::vector<fastring>" in engine and "#include <vector>" not in engine:
        # A `string[]` is a vector of fastrings; lists alone include it.
        k = engine.index("#include <stdint.h>\n") + len("#include <stdint.h>\n")
        engine = engine[:k] + "#include <vector>\n" + engine[k:]
    if re.search(r"\bfastring\b", engine):
        croot = require_coost_root(coost_root)
        anchor = "#include <stdint.h>\n"
        k = engine.index(anchor) + len(anchor)
        # coost's hashes, when the engine calls into them.
        extra = [src for fn, src in (("md5digest_to", "src/hash/md5.cc"),
                                     ("sha256digest_to", "src/hash/sha256.cc"),
                                     ("base64_", "src/hash/base64.cc"))
                 if fn in engine]
        engine = engine[:k] + coost_string_core(croot, extra) + engine[k:]
        plan["coost_root"] = croot
    if gpu_handles:
        # Packed handle streams for a GLES 3.1 SSBO (`_handle_streams`).
        engine += emit_handles_c(plan)
    _progress("emitting data.c (%d texture(s))" % len(plan.get("textures") or []))
    data = emit_data(plan, used_apis)
    main_c = emit_main()
    # C++-subset twins: same text, fed through cpprust then crust (csrust pipe).
    engine_cpp = _cpp_twin(engine)
    data_cpp = _cpp_twin(data)
    main_cpp = _cpp_twin(main_c)

    def _validate_or_reuse(cpp_text, cpp_name, c_name, c_fallback):
        """Transpile when *cpp_text* changed; else reuse on-disk lowered .c."""
        if _emit_artifact_unchanged(outdir, cpp_name, c_name, cpp_text, force):
            _progress("unchanged %s; skipping cpprust + crust" % cpp_name)
            with open(os.path.join(outdir, c_name)) as f:
                return f.read()
        _progress("validating %s through cpprust + crust" % cpp_name)
        out = validate_emitted_c(cpp_text, cpp_name, analyses=analyses)
        if out is None:
            out = c_fallback
        _write_if_different(os.path.join(outdir, cpp_name), cpp_text)
        _write_if_different(os.path.join(outdir, c_name), out)
        return out

    engine_c = _validate_or_reuse(engine_cpp, "engine.cpp", "engine.c", engine)
    data_c_out = _validate_or_reuse(data_cpp, "data.cpp", "data.c", data)
    main_c_out = _validate_or_reuse(main_cpp, "main.cpp", "main.c", main_c)
    _progress("writing %s" % outdir)
    # Ensure .c present even if reuse path already had them (no-op write).
    _write_if_different(os.path.join(outdir, "engine.c"), engine_c)
    _write_if_different(
        os.path.join(outdir, "data.c"),
        data_c_out if data_c_out is not None else data)
    _write_if_different(os.path.join(outdir, "main.c"), main_c_out)
    _write_if_different(os.path.join(outdir, "engine.cpp"), engine_cpp)
    _write_if_different(os.path.join(outdir, "data.cpp"), data_cpp)
    _write_if_different(os.path.join(outdir, "main.cpp"), main_cpp)
    _write_if_different(
        os.path.join(outdir, "engine_draw.h"), emit_engine_draw_h())
    _write_if_different(
        os.path.join(outdir, "Makefile"),
        emit_makefile(outdir, box2d=bool(plan.get("box2d_physics")),
                      box2d_inject=bool(physics_inject),
                      gpu_physics=bool(gpu_physics and plan.get("box2d_physics"))))
    if plan.get("box2d_physics") and gpu_physics:
        import tools.unity.unity_pack_gpu_physics as _gpuphys
        _gpuphys.emit(outdir, plan, gpu_physics)
        for stale in ("physics_box2d.c", "box2d_inject.json"):
            if os.path.exists(os.path.join(outdir, stale)):
                os.remove(os.path.join(outdir, stale))
    elif plan.get("box2d_physics"):
        b2u = _load_box2d_unity(box2d_root)
        if plan.get("physics2d_contacts") and not hasattr(
                b2u, "_with_contact_manifolds"):
            raise PackError(
                "Collision2D contacts (GetContact / contactCount) need a "
                "Box2D-Packed checkout that reports manifolds; update %s"
                % os.path.dirname(b2u.__file__))
        if plan.get("godot"):
            if plan.get("physics2d_layers") and not hasattr(
                    b2u, "_with_godot_layers"):
                raise PackError(
                    "collision layers need a Box2D-Packed checkout with "
                    "Godot's layer filter; update %s"
                    % os.path.dirname(b2u.__file__))
            if "godot" not in getattr(b2u, "MODES", ()):
                raise PackError(
                    "this Box2D-Packed checkout (%s) has no Godot mode; "
                    "update it" % os.path.dirname(b2u.__file__))
            b2u.emit_glue(
                outdir, plan, inject=bool(physics_inject), mode="godot",
                length_units_per_meter=plan["godot"]["length_units_per_meter"])
        else:
            b2u.emit_glue(outdir, plan, inject=bool(physics_inject))
    else:
        for stale in ("physics_box2d.c", "box2d_inject.json"):
            if os.path.exists(os.path.join(outdir, stale)):
                os.remove(os.path.join(outdir, stale))
    if not (gpu_physics and plan.get("box2d_physics")) and os.path.exists(
            os.path.join(outdir, "physics_gpu.c")):
        os.remove(os.path.join(outdir, "physics_gpu.c"))
    shdir = os.path.join(outdir, "shaders")
    os.makedirs(shdir, exist_ok=True)
    for plat in ("linux", "apple", "windows", "wasm"):
        _write_if_different(
            os.path.join(shdir, "shader_compiler_%s.c" % plat),
            emit_shader_compiler(plat))
    _write_if_different(
        os.path.join(shdir, "soa_positions.glsl"),
        emit_soa_positions_glsl(plan))
    if gpu_handles:
        _write_if_different(os.path.join(outdir, "engine_handles.h"),
                            emit_handles_h(plan))
        _write_if_different(os.path.join(shdir, "handles.glsl"),
                            emit_handles_glsl(plan))
    _write_stamp(outdir, {
        "version": _STAMP_VERSION,
        "input_fingerprint": fp,
        "assets_fingerprint": assets_fp,
        "scripts_fingerprint": scripts_fp,
        "soa": bool(soa),
        "soa_vec4": bool(soa_vec4),
        "physics": physics_key,
        "product_name": plan.get("product_name") or "Player",
        "two_d": bool(plan.get("two_d")),
        "classes": _class_stamp_entries(plan),
        "outputs": {
            "engine.cpp": _sha256_text(engine_cpp),
            "data.cpp": _sha256_text(data_cpp),
            "main.cpp": _sha256_text(main_cpp),
        },
    })
    _progress("done")
    return plan


def project_folder_name(root):
    """Unity project folder name (not productName)."""
    return os.path.basename(os.path.abspath(root).rstrip(os.sep)) or "Player"



def default_pack_dir(root):
    """$TMPDIR/<project folder> — default place for sources and the player."""
    import tempfile
    return os.path.join(tempfile.gettempdir(), project_folder_name(root))



def main():
    args = list(sys.argv[1:])
    outdir = None
    soa = True
    soa_vec4 = False
    force = False
    strict = None   # by project: strict for Godot (see _pack_impl)
    if "--force" in args:
        force = True
        args.remove("--force")
    if "--strict" in args:
        strict = True
        args.remove("--strict")
    gpu_handles = False
    if "--gpu-handles" in args:
        gpu_handles = True
        args.remove("--gpu-handles")
    hybrid = False
    if "--hybrid" in args:
        hybrid = True
        args.remove("--hybrid")
    managed = None
    for a in list(args):
        if a == "--managed":
            managed = "*"
            args.remove(a)
        elif a.startswith("--managed="):
            names = [n.strip() for n in a.split("=", 1)[1].split(",") if n.strip()]
            managed = "*" if "*" in names else (set(names) | (managed if isinstance(managed, set) else set()))
            args.remove(a)
    packed = False
    if "--packed" in args:
        packed = True
        args.remove("--packed")
    engine = False
    if "--engine" in args:
        engine = True
        args.remove("--engine")
    gpu_batch = False
    if "--gpu-batch" in args:
        gpu_batch = True
        args.remove("--gpu-batch")
    if "--soa" in args:
        sys.stderr.write(
            "unity_pack: --soa is gone; SoA positions are the default. "
            "Use --aos for AoS structs, or --soa-vec4 for float[N][4].\n")
        return 2
    if "--soa-vec4" in args:
        soa_vec4 = True
        soa = True
        args.remove("--soa-vec4")
    if "--aos" in args:
        soa = False
        soa_vec4 = False
        args.remove("--aos")
    if "--physics" in args:
        sys.stderr.write(
            "unity_pack: --physics is gone; 2D physics is always Box2D-Packed "
            "(--box2d PATH, $BOX2D_PACKED_ROOT, or ../box2d).\n")
        return 2
    physics_inject = False
    if "--physics-inject" in args:
        physics_inject = True
        args.remove("--physics-inject")
    box2d_lto = False
    if "--box2d-lto" in args:
        box2d_lto = True
        args.remove("--box2d-lto")
    coost_root = None
    if "--coost" in args:
        i = args.index("--coost")
        if i + 1 >= len(args):
            sys.stderr.write("unity_pack: --coost needs a coost checkout\n")
            return 2
        coost_root = args[i + 1]
        del args[i:i + 2]
    gpu_physics = None
    if "--gpu-physics" in args:
        i = args.index("--gpu-physics")
        gpu_physics = True
        if i + 1 < len(args) and os.path.isfile(
                os.path.join(args[i + 1], "runtime", "gpuphys.c")):
            gpu_physics = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
    box2d_root = None
    if "--box2d" in args:
        i = args.index("--box2d")
        if i + 1 >= len(args):
            sys.stderr.write("unity_pack: --box2d needs a Box2D-Packed checkout\n")
            return 2
        box2d_root = args[i + 1]
        del args[i:i + 2]
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
            "[--aos | --soa-vec4] [--force] [--strict] [--gpu-handles]\n"
            "       [--physics-inject] [--box2d PATH] [--box2d-lto] [--gpu-physics [PATH]]"
            " [--coost PATH] [--hybrid] [--managed[=Class,..]]\n"
            "  default out-dir: $TMPDIR/<project folder>\n"
            "  player binary:   <productName>  (Windows: <productName>.exe)\n"
            "  default layout:  SoA position tables (use --aos for AoS)\n"
            "  --force:         ignore stamp; always re-emit and transpile\n"
            "  --gpu-physics:   2D physics on the gpu-physics-engine circle solver, not Box2D\n"
            "  --hybrid:        a method that cannot be lowered to C runs as managed\n"
            "                   code on DotNetAnywhere (needs ../DotNetAnywhere, and\n"
            "                   ../CCSharp built (Roslyn) or mcs)\n"
            "  --managed[=A,B]: run whole script classes (all, or the named ones) as\n"
            "                   managed C# on DotNetAnywhere instead of lowering them;\n"
            "                   a class that cannot be built managed stays lowered\n")
        return 2
    if outdir is None:
        outdir = default_pack_dir(args[0])
    if engine and not packed:
        # CCSharp backend (tools/unity/unity_pack_ccs.py); falls back to the packed path when the engine does not cover the project.
        import tools.unity.unity_pack_ccs as _ccs
        if not _ccs.available():
            sys.stderr.write("unity_pack: --engine needs CCSharp beside this repo (or $CCSHARP_HOME); using the packed path\n")
        else:
            _assets = _asset_guid_map(args[0])
            objs, _l, cams, _h = _load_scenes_lights_cameras(args[0], _assets)
            prefabs = _ccs.collect_prefabs(args[0], objs, _assets)
            ok, why = _ccs.eligible(objs, cams, prefabs)
            if ok:
                os.makedirs(outdir, exist_ok=True)
                return _ccs.build(objs, cams, outdir, run=False, prefabs=prefabs, materials=_load_physics_materials(_assets)[0])
            sys.stderr.write("unity_pack: --engine: %s; using the packed path\n" % why)
    try:
        plan = pack(args[0], outdir, soa=soa, soa_vec4=soa_vec4, force=force,
                    strict=strict, gpu_handles=gpu_handles, gpu_batch=gpu_batch,
                    physics_inject=physics_inject, box2d_root=box2d_root,
                    coost_root=coost_root, hybrid=hybrid, managed=managed,
                    gpu_physics=gpu_physics)
        exe = build_player_executable(
            outdir, plan.get("product_name") or "Player",
            box2d_root=box2d_root, box2d_lto=box2d_lto)
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
                         % (name, cl.get("n", 0), cl.get("size", 0),
                            cl.get("idx_ty", "int"), extra))
    return 0


# The emitter's code finds what unity_pack defined after the import above (and what it rebinds) in unity_pack's namespace:
for _k, _v in list(globals().items()):
    if not _k.startswith("__") and _k not in _EMIT_OWN:
        setattr(_emit_mod, _k, _v)

if __name__ == "__main__":
    sys.exit(main())
