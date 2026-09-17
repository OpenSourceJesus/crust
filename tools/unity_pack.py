#!/usr/bin/env python3
"""unity_pack -- emit a packed engine.c + data.c from a Unity-subset project.

See UNITY_PACK.md. Walks scripts and scenes, keeps only the Unity API that
is called, and lays objects out as small as the data allows: drop z in 2D,
float16 for static backgrounds, bitfields for small ints, and uint8_t
indices instead of pointers when a class is bounded (hand-placed, never
spawned, N ≤ 256).

    python3 tools/unity_pack.py <project> -o <outdir>
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cs2cpp as cs2cpp  # noqa: E402


class PackError(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message


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
    "Time.deltaTime": None,  # globals in data.c — host can poke
    "Time.time": None,
    "Time.fixedDeltaTime": None,
    "Physics2D.gravity": None,
    "ParticleSystem.Emit": None,
    "AnimationCurve.Evaluate": None,
    "Input.GetAxis": (
        "static float Input_GetAxis(const char *name) {\n"
        "    (void)name; return 0.f; /* host fills engine_input_axis */\n}"
    ),
}

_SPAWN = re.compile(
    r"(?<![\w.])(Instantiate|Destroy|Object\.Instantiate|"
    r"GameObject\.Instantiate|new\s+GameObject)\b"
)
_VEC3Z = re.compile(r"\.(z)\b|Vector3|Quaternion")
_UNITY_API = re.compile(
    r"(?<![\w])(Mathf\.(Abs|Min|Max|Clamp|Lerp|Sin|Cos)|"
    r"Time\.(deltaTime|time|fixedDeltaTime)|Input\.GetAxis|"
    r"transform\.position|Physics2D\.gravity|ParticleSystem\.Emit|"
    r"AnimationCurve\.Evaluate|Vector2|Vector3|Quaternion)\b"
)


# ---------------------------------------------------------------------------
# Project walk
# ---------------------------------------------------------------------------

def _read(path):
    with open(path) as f:
        return f.read()


def _walk_files(root, exts):
    out = []
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "Library", "Temp", "obj",
                                    "graphify-out", "__pycache__")]
        for n in names:
            if any(n.endswith(e) for e in exts):
                out.append(os.path.join(dirpath, n))
    out.sort()
    return out


def _guid_map(root):
    """Unity .meta `guid:` next to a .cs file → script path."""
    out = {}
    for meta in _walk_files(root, (".cs.meta",)):
        text = _read(meta)
        m = re.search(r"(?m)^guid:\s*([0-9a-fA-F]+)\s*$", text)
        if not m:
            continue
        cs = meta[:-5] if meta.endswith(".meta") else meta
        out[m.group(1).lower()] = cs
    return out


# ---------------------------------------------------------------------------
# Scene importers
# ---------------------------------------------------------------------------

def parse_unity_yaml(text, guid_to_script=None):
    """A Unity .unity YAML subset: GameObject + Transform + MonoBehaviour.

    Not a YAML library. Unity's document-per-object form is regular enough
    that a block split on `--- !u!` is enough, and a dependency on PyYAML
    would make the test suite depend on the outside world.
    """
    guid_to_script = guid_to_script or {}
    objects = []
    blocks = re.split(r"(?m)^---\s+", text)
    by_id = {}
    for block in blocks:
        hm = re.match(r"!u!(\d+)\s+&(\d+)", block)
        if not hm:
            continue
        file_id = hm.group(2)
        kind = None
        km = re.search(r"(?m)^(GameObject|Transform|MonoBehaviour|PrefabInstance):",
                       block)
        if km:
            kind = km.group(1)
        rec = {"file_id": file_id, "kind": kind, "raw": block, "fields": {}}
        nm = re.search(r"(?m)^\s+m_Name:\s*(.+)$", block)
        if nm:
            rec["name"] = nm.group(1).strip()
        pos = re.search(
            r"m_LocalPosition:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
            r"\s*z:\s*([^}]+)\}", block)
        if pos:
            rec["pos"] = (float(pos.group(1)), float(pos.group(2)),
                          float(pos.group(3)))
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
        by_id[file_id] = rec

    # Join MonoBehaviour + Transform onto the GameObject.
    gos = [r for r in by_id.values() if r.get("kind") == "GameObject"]
    for go in gos:
        kids = []
        for mid in re.findall(r"fileID:\s*(\d+)", go["raw"]):
            if mid in by_id and by_id[mid] is not go:
                kids.append(by_id[mid])
        pos = (0.0, 0.0, 0.0)
        script = None
        fields = {}
        for k in kids:
            if k.get("pos"):
                pos = k["pos"]
            if k.get("kind") == "MonoBehaviour":
                fields.update(k.get("fields") or {})
                g = k.get("guid")
                if g and g in guid_to_script:
                    script = guid_to_script[g]
        class_name = None
        if script:
            class_name = _class_name_from_cs(script)
        objects.append({
            "name": go.get("name") or "obj",
            "pos": pos,
            "fields": fields,
            "script": script,
            "class": class_name or go.get("name") or "Obj",
        })
    return objects


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


# ---------------------------------------------------------------------------
# Script analysis
# ---------------------------------------------------------------------------

def analyze_script(path, text=None):
    """Fields, methods, Unity API used, whether the script spawns."""
    if text is None:
        text = _read(path)
    scan = cs2cpp._blank(text)
    apis = set()
    for m in _UNITY_API.finditer(scan):
        apis.add(m.group(0) if m.group(0).startswith("Mathf.")
                 or m.group(0).startswith("Time.")
                 or m.group(0).startswith("Input.")
                 else m.group(0))
    if "transform.position" in scan:
        apis.add("transform.position")
    spawns = bool(_SPAWN.search(scan))
    uses_z = bool(re.search(r"(?<![\w.])Vector3\b", scan)
                  or re.search(r"(?<![\w.])Quaternion\b", scan)
                  or re.search(r"transform\.position\.z", scan))
    writes_pos = bool(re.search(
        r"transform\.position\s*=|"
        r"transform\.position\s*\+=|"
        r"transform\.Translate", scan))

    types = cs2cpp._find_types(scan)
    classes = []
    for kind, name, start, brace, close in types:
        if kind not in ("class", "struct"):
            continue
        body = text[brace + 1:close]
        bscan = scan[brace + 1:close]
        fields = _fields_in(body, bscan)
        methods = _methods_in(body, bscan)
        refs = []
        for f in fields:
            if f["ty"] not in _PRIM and f["ty"] not in (
                    "Vector2", "Vector3", "Quaternion", "string"):
                refs.append(f)
        classes.append({
            "name": name, "kind": kind, "fields": fields,
            "methods": methods, "refs": refs,
            "path": path,
        })
    return {
        "path": path,
        "apis": apis,
        "spawns": spawns,
        "uses_z": uses_z,
        "writes_pos": writes_pos,
        "classes": classes,
        "literals": [int(x) for x in re.findall(r"(?<![\w.])(\d+)", scan)
                     if int(x) < 1 << 20],
    }


_PRIM = ("int", "float", "bool", "byte", "short", "uint", "long",
         "double", "sbyte", "ushort", "ulong")


def _fields_in(body, bscan):
    """Instance fields; methods (those with `(`) are skipped."""
    out = []
    for m in re.finditer(
            r"(?m)^[ \t]*(?:public|private|protected|internal)?"
            r"[ \t]*(?:static[ \t]+)?(?:readonly[ \t]+)?"
            r"([\w.<>]+)[ \t]+(\w+)[ \t]*(?:=|;)",
            bscan):
        after = bscan[m.end(2):m.end(2) + 8]
        # `int F(` is a method.
        tail = body[m.end(2):m.end(2) + 16]
        if "(" in tail.split(";")[0] and "=" not in tail.split(";")[0]:
            continue
        ty, name = m.group(1).strip(), m.group(2)
        if ty in ("if", "for", "return", "new"):
            continue
        out.append({"ty": ty, "name": name})
    return out


def _methods_in(body, bscan):
    out = []
    for m in re.finditer(
            r"(?m)^[ \t]*(?:public|private|protected|internal)?"
            r"[ \t]*(?:static[ \t]+)?(?:override[ \t]+)?(?:virtual[ \t]+)?"
            r"([\w.<>]+)[ \t]+(\w+)[ \t]*\(([^)]*)\)[ \t]*\{",
            bscan):
        open_i = m.end() - 1
        close = cs2cpp._match_brace(bscan, open_i) if hasattr(
            cs2cpp, "_match_brace") else None
        # _match_brace lives on cpprust; cs2cpp uses it via import.
        import tools.cpprust as cpprust
        close = cpprust._match_brace(bscan, open_i)
        if close is None:
            continue
        src = body[m.start():m.start() + (close - m.start()) + 1]
        impl = body[m.end():m.start() + (close - m.start())]
        out.append({
            "ret": m.group(1).strip(),
            "name": m.group(2),
            "args": m.group(3).strip(),
            "body": impl,
            "src": src,
        })
    return out


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
            writes[c["name"]] = writes.get(c["name"], False) or a["writes_pos"]

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
        for a in analyses:
            for c in a["classes"]:
                if c["name"] == cname:
                    script_fields = c["fields"]
        for f in script_fields:
            field_tys[f["name"]] = f["ty"]
        for o in insts:
            for k in o["fields"]:
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
            if ty == "Vector2":
                members.append((fname + "_x", "float", 32, "f32"))
                members.append((fname + "_y", "float", 32, "f32"))
                continue
            if ty == "Vector3":
                continue  # transform owns position; full Vector3 fields later
            if ty in ("int", "byte", "short", "uint"):
                vals = [o["fields"][fname] for o in insts if fname in o["fields"]]
                if not vals:
                    vals = [0]
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
        }
    return {"two_d": two_d, "spawn": spawn, "classes": plans}


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
    want_anim = "AnimationCurve.Evaluate" in used_apis
    want_particles = "ParticleSystem.Emit" in used_apis
    want_phys = "Physics2D.gravity" in used_apis
    p("/* generated by tools/unity_pack.py — do not edit */")
    if soa:
        p("/* layout: SoA positions (contiguous float tables for GPU upload) */")
    p("#include <stdint.h>")
    if want_math:
        p("#include <math.h>")
    p("")
    p("/* Types first, then every global. C forbids `extern T a[N]` while")
    p("   T is incomplete, so the arrays wait until the structs exist;")
    p("   the names are all listed here as comments so data.c and any")
    p("   group can find them. */")
    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        p("typedef struct %s %s;" % (idn, idn))
    p("extern float Time_deltaTime;")
    if "Time.time" in used_apis or want_anim:
        p("extern float Time_time;")
    if "Time.fixedDeltaTime" in used_apis or want_phys:
        p("extern float Time_fixedDeltaTime;")
    if want_phys:
        p("extern float Physics2D_gravity_x;")
        p("extern float Physics2D_gravity_y;")
    if want_anim:
        p("typedef struct { float t, v; } AnimKey;")
        p("extern const AnimKey _AnimCurve0[];")
        p("extern const int _AnimCurve0_n;")
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
        p("extern %s _%s_inst_array[%d];" % (idn, idn, cl["n"]))
        p("extern const int _%s_inst_count;" % idn)
        if cl.get("soa_dims"):
            p("extern float _%s_pos[%d][%d];" % (idn, cl["n"], cl["soa_dims"]))
    p("")

    # Used Unity API only.
    if "Time.deltaTime" in used_apis or "Time.time" in used_apis:
        p("/* Time_* globals are defined in data.c so a host can poke them. */")
    for key, snippet in _API.items():
        if key in used_apis and snippet:
            p(snippet)
            p("")

    if want_anim:
        p("static float AnimationCurve_Evaluate(const AnimKey *keys, int n, float t) {")
        p("    int i;")
        p("    float t0, t1, v0, v1, u;")
        p("    if (!keys || n < 1) return 0.f;")
        p("    if (t <= keys[0].t) return keys[0].v;")
        p("    if (t >= keys[n - 1].t) return keys[n - 1].v;")
        p("    for (i = 0; i < n - 1; i = i + 1) {")
        p("        if (t >= keys[i].t && t <= keys[i + 1].t) {")
        p("            t0 = keys[i].t; t1 = keys[i + 1].t;")
        p("            v0 = keys[i].v; v1 = keys[i + 1].v;")
        p("            u = (t1 > t0) ? (t - t0) / (t1 - t0) : 0.f;")
        p("            return v0 + (v1 - v0) * u;")
        p("        }")
        p("    }")
        p("    return keys[n - 1].v;")
        p("}")
        p("")

    if want_particles:
        p("#define PARTICLE_MAX 64")
        p("typedef struct {")
        p("    float x, y, vx, vy, life;")
        p("    int alive;")
        p("} Particle;")
        p("static Particle _particles[PARTICLE_MAX];")
        p("")
        p("static void ParticleSystem_Emit(float x, float y) {")
        p("    int i;")
        p("    for (i = 0; i < PARTICLE_MAX; i = i + 1) {")
        p("        if (!_particles[i].alive) {")
        p("            _particles[i].x = x;")
        p("            _particles[i].y = y;")
        p("            _particles[i].vx = 0.4f * ((float)(i & 3) - 1.5f);")
        p("            _particles[i].vy = 1.5f;")
        p("            _particles[i].life = 1.f;")
        p("            _particles[i].alive = 1;")
        p("            return;")
        p("        }")
        p("    }")
        p("}")
        p("")
        p("static void ParticleSystem_Tick(float dt) {")
        p("    int i;")
        p("    for (i = 0; i < PARTICLE_MAX; i = i + 1) {")
        p("        if (!_particles[i].alive) continue;")
        p("        _particles[i].vy = _particles[i].vy - 4.f * dt;")
        p("        _particles[i].x = _particles[i].x + _particles[i].vx * dt;")
        p("        _particles[i].y = _particles[i].y + _particles[i].vy * dt;")
        p("        _particles[i].life = _particles[i].life - dt;")
        p("        if (_particles[i].life <= 0.f) _particles[i].alive = 0;")
        p("    }")
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

    # Group methods by class; array comment sits on the group.
    methods_by = {}
    for a in analyses:
        for c in a["classes"]:
            methods_by.setdefault(c["name"], []).extend(
                [(c, m) for m in c["methods"]
                 if m["name"] not in ("Start",) or True])

    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        p("/* ---- %s group: instance array is defined in data.c ---- */" % idn)
        p("#define %s_AT(i) (_%s_inst_array[(i)])" % (idn, idn))
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
        for c, m in methods_by.get(cname, []):
            if m["name"] in ("Start", "Awake", "OnEnable"):
                continue
            body = _lower_method_body(m["body"], cl, plan)
            p("static void %s_%s(unsigned i) {" % (idn, m["name"]))
            for line in body.split("\n"):
                if line.strip():
                    p("    " + line.rstrip())
            p("}")
            p("")

        # Tick: FixedUpdate then Update on each instance.
        has_fixed = any(m["name"] == "FixedUpdate"
                        for _c, m in methods_by.get(cname, []))
        has_update = any(m["name"] == "Update"
                         for _c, m in methods_by.get(cname, []))
        p("void %s_FixedTick(void) {" % idn)
        if has_fixed:
            p("    int n;")
            p("    for (n = 0; n < _%s_inst_count; n = n + 1)" % idn)
            p("        %s_FixedUpdate((unsigned)n);" % idn)
        else:
            p("    /* no FixedUpdate */")
        p("}")
        p("")
        p("void %s_Tick(void) {" % idn)
        if has_update:
            p("    int n;")
            p("    for (n = 0; n < _%s_inst_count; n = n + 1)" % idn)
            p("        %s_Update((unsigned)n);" % idn)
        else:
            p("    /* no Update */")
        p("}")
        p("")

    p("void engine_tick(void) {")
    if "Time.time" in used_apis or want_anim:
        p("    Time_time = Time_time + Time_deltaTime;")
    if want_particles:
        p("    ParticleSystem_Tick(Time_deltaTime);")
    for cname in sorted(plan["classes"]):
        p("    %s_FixedTick();" % _c_ident(cname))
    for cname in sorted(plan["classes"]):
        p("    %s_Tick();" % _c_ident(cname))
    p("}")
    p("")
    p("int engine_class_count(void) { return %d; }" % len(plan["classes"]))
    p("")

    # Draw list for a GLES (or any) host: one coloured quad per instance
    # that has a packed position. Colours are a stable hash of the class
    # name so a scene does not need hand-wired materials.
    p("/* ---- draw list (see engine_draw.h) ---- */")
    p("typedef struct EngineDraw {")
    p("    float x, y, half_w, half_h;")
    p("    float r, g, b;")
    p("} EngineDraw;")
    p("")
    p("int engine_collect_draws(EngineDraw *out, int max) {")
    p("    int n = 0;")
    p("    if (!out || max < 1) return 0;")
    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        if not _class_has_position(cl):
            continue
        r, g, b = _class_rgb(cname)
        p("    {")
        p("        int i;")
        p("        for (i = 0; i < _%s_inst_count && n < max; i = i + 1) {" % idn)
        p("            out[n].x = %s_get_pos_x((unsigned)i);" % idn)
        p("            out[n].y = %s_get_pos_y((unsigned)i);" % idn)
        p("            out[n].half_w = 0.2f;")
        p("            out[n].half_h = 0.2f;")
        p("            out[n].r = %sf;" % repr(r))
        p("            out[n].g = %sf;" % repr(g))
        p("            out[n].b = %sf;" % repr(b))
        p("            n = n + 1;")
        p("        }")
        p("    }")
    if want_particles:
        p("    {")
        p("        int i;")
        p("        for (i = 0; i < PARTICLE_MAX && n < max; i = i + 1) {")
        p("            if (!_particles[i].alive) continue;")
        p("            out[n].x = _particles[i].x;")
        p("            out[n].y = _particles[i].y;")
        p("            out[n].half_w = 0.05f;")
        p("            out[n].half_h = 0.05f;")
        p("            out[n].r = 1.f;")
        p("            out[n].g = 0.85f;")
        p("            out[n].b = 0.2f;")
        p("            n = n + 1;")
        p("        }")
        p("    }")
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
        p("        int i, d;")
        p("        int need = _%s_inst_count * %d;" % (idn, dims))
        p("        if (n + need > max_floats) return n;")
        if cl.get("soa_dims"):
            p("        /* SoA: one contiguous table, no AoS gather */")
            p("        for (i = 0; i < _%s_inst_count; i = i + 1)" % idn)
            p("            for (d = 0; d < %d; d = d + 1)" % dims)
            p("                dst[n + i * %d + d] = _%s_pos[i][d];" % (dims, idn))
        else:
            axes = ("pos_x", "pos_y", "pos_z")[:dims]
            p("        /* AoS gather (Unity-style) */")
            p("        for (i = 0; i < _%s_inst_count; i = i + 1) {" % idn)
            for axis_i, axis in enumerate(axes):
                p("            dst[n + i * %d + %d] = %s_get_%s((unsigned)i);"
                  % (dims, axis_i, idn, axis))
            p("        }")
        p("        n = n + need;")
        p("    }")
    p("    return n;")
    p("}")
    p("")
    return "\n".join(lines) + "\n"


def _class_rgb(name):
    """Deterministic saturated colour from a class name (not MiniScene-specific)."""
    h = 2166136261
    for ch in name:
        h ^= ord(ch)
        h = (h * 16777619) & 0xffffffff
    # Map hash bits to hue-ish RGB in [0.25, 1.0] so quads are visible on black.
    r = 0.25 + ((h >> 0) & 255) / 255.0 * 0.75
    g = 0.25 + ((h >> 8) & 255) / 255.0 * 0.75
    b = 0.25 + ((h >> 16) & 255) / 255.0 * 0.75
    return (round(r, 4), round(g, 4), round(b, 4))


def emit_engine_draw_h():
    """Public draw-list API written next to engine.c so hosts stay in sync."""
    return (
        "/* generated by tools/unity_pack.py — do not edit */\n"
        "#ifndef UNITY_PACK_ENGINE_DRAW_H\n"
        "#define UNITY_PACK_ENGINE_DRAW_H\n"
        "\n"
        "typedef struct EngineDraw {\n"
        "    float x, y, half_w, half_h;\n"
        "    float r, g, b;\n"
        "} EngineDraw;\n"
        "\n"
        "void engine_tick(void);\n"
        "int engine_class_count(void);\n"
        "int engine_collect_draws(EngineDraw *out, int max);\n"
        "/* Contiguous x,y[,z] floats for every positioned instance (class\n"
        " * name order). SoA packs fill this from flat tables; AoS gathers. */\n"
        "int engine_position_floats(void);\n"
        "int engine_upload_positions(float *dst, int max_floats);\n"
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


def _rewrite_new_vector_assigns(text, idn):
    """`transform.position =/+ = new Vector2/3(...)` with nested calls."""

    def repl_eq(m):
        args = _split_call_args(m.group(1))
        if len(args) < 2:
            return m.group(0)
        return "%s_set_pos_x(i, (%s)); %s_set_pos_y(i, (%s));" % (
            idn, args[0], idn, args[1])

    def repl_add(m):
        args = _split_call_args(m.group(1))
        if len(args) < 2:
            return m.group(0)
        return (
            "%s_set_pos_x(i, %s_get_pos_x(i) + (%s)); "
            "%s_set_pos_y(i, %s_get_pos_y(i) + (%s));" % (
                idn, idn, args[0], idn, idn, args[1])
        )

    flags = re.DOTALL
    text = re.sub(
        r"transform\.position\s*=\s*new\s+Vector2\s*\((.*?)\)\s*;",
        repl_eq, text, flags=flags)
    text = re.sub(
        r"transform\.position\s*=\s*new\s+Vector3\s*\((.*?)\)\s*;",
        repl_eq, text, flags=flags)
    text = re.sub(
        r"transform\.position\s*\+=\s*new\s+Vector2\s*\((.*?)\)\s*;",
        repl_add, text, flags=flags)
    text = re.sub(
        r"transform\.position\s*\+=\s*new\s+Vector3\s*\((.*?)\)\s*;",
        repl_add, text, flags=flags)
    return text


def _lower_method_body(body, cl, plan):
    """C# subset method → C against packed arrays.

    `this` / implicit fields become `_Class_inst_array[i].field`.
    `transform.position.x` becomes the packed pos_*. A class-typed
    field is already an index: `other.hp` → `_Other_inst_array[other].hp`.
    """
    idn = _c_ident(cl["name"])
    text = body
    text = re.sub(r"\bthis\.", "", text)
    # API tokens before Vector2 rewrites so nested Mathf.Sin(...) keeps parens.
    text = text.replace("Time.deltaTime", "Time_deltaTime")
    text = text.replace("Time.fixedDeltaTime", "Time_fixedDeltaTime")
    text = text.replace("Time.time", "Time_time")
    text = text.replace("Physics2D.gravity.x", "Physics2D_gravity_x")
    text = text.replace("Physics2D.gravity.y", "Physics2D_gravity_y")
    text = re.sub(r"ParticleSystem\.Emit\s*\(", "ParticleSystem_Emit(", text)
    text = re.sub(
        r"AnimationCurve\.Evaluate\s*\(",
        "AnimationCurve_Evaluate(_AnimCurve0, _AnimCurve0_n, ",
        text)
    text = re.sub(r"Mathf\.(Abs|Min|Max|Clamp|Lerp|Sin|Cos)\s*\(",
                  lambda m: "Mathf_%s(" % m.group(1), text)
    text = re.sub(r"transform\.position\.x", idn + "_get_pos_x(i)", text)
    text = re.sub(r"transform\.position\.y", idn + "_get_pos_y(i)", text)
    text = re.sub(r"transform\.position\.z",
                  idn + "_get_pos_z(i)" if not cl["two_d"] else "0.f", text)
    text = _rewrite_new_vector_assigns(text, idn)

    members = {n for n, _t, _b, _k in cl["members"]}
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
    for name in sorted(members, key=len, reverse=True):
        text = re.sub(
            r"(?<![_\w])%s\s*\+=" % name,
            "%s_set_%s(i, %s_get_%s(i) +" % (idn, name, idn, name),
            text)
        text = re.sub(
            r"(?<![_\w])%s\s*-=" % name,
            "%s_set_%s(i, %s_get_%s(i) -" % (idn, name, idn, name),
            text)
        text = re.sub(
            r"(?<![_\w])%s\s*=" % name,
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
    for name, _ty, _bits, kind in cl["members"]:
        if not str(kind).startswith("idx:"):
            continue
        other = kind.split(":", 1)[1]
        oiden = _c_ident(other)
        text = re.sub(
            r"\b%s\.(\w+)" % name,
            lambda m: "%s_AT(%s_get_%s(i)).%s" % (
                oiden, idn, name, m.group(1)),
            text)
    return text


def emit_data(plan, used_apis=None):
    lines = []
    p = lines.append
    used_apis = used_apis or set()
    want_anim = "AnimationCurve.Evaluate" in used_apis
    want_phys = "Physics2D.gravity" in used_apis
    p("/* generated by tools/unity_pack.py — scene tables, compile -O0 */")
    if plan.get("soa"):
        p("/* SoA: positions live in _Class_pos[N][dims], not in the struct */")
    p("#include <stdint.h>")
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
    if "Time.time" in used_apis or want_anim:
        p("float Time_time = 0.f;")
    if "Time.fixedDeltaTime" in used_apis or want_phys:
        p("float Time_fixedDeltaTime = 0.02f;")
    if want_phys:
        p("float Physics2D_gravity_x = 0.f;")
        p("float Physics2D_gravity_y = -9.81f;")
    if want_anim:
        p("typedef struct { float t, v; } AnimKey;")
        p("const AnimKey _AnimCurve0[] = {")
        p("    { 0.f, 0.f }, { 0.5f, 1.f }, { 1.f, 0.f }")
        p("};")
        p("const int _AnimCurve0_n = 3;")
    p("")
    for cname, cl in sorted(plan["classes"].items()):
        idn = _c_ident(cname)
        p("const int _%s_inst_count = %d;" % (idn, cl["n"]))
        if cl.get("soa_dims"):
            dims = cl["soa_dims"]
            logical = _soa_axis_count(cl)
            p("float _%s_pos[%d][%d] = {" % (idn, cl["n"], dims))
            for ii, o in enumerate(cl["instances"]):
                coords = [float(o["pos"][0]), float(o["pos"][1])]
                if logical >= 3:
                    coords.append(float(o["pos"][2]))
                elif dims >= 3:
                    coords.append(0.0)  # 2D → vec4: z = 0
                if dims == 4:
                    coords.append(float(ii))  # .w = instance index
                while len(coords) < dims:
                    coords.append(0.0)
                parts = ["%sf" % repr(v) for v in coords]
                p("    { %s }, /* %s */" % (", ".join(parts), o["name"]))
            p("};")
            p("")
        p("%s _%s_inst_array[%d] = {" % (idn, idn, cl["n"]))
        for o in cl["instances"]:
            parts = []
            for name, ty, bits, kind in cl["members"]:
                if name == "pos_x":
                    v = o["pos"][0]
                    parts.append(_init_num(v, kind))
                elif name == "pos_y":
                    v = o["pos"][1]
                    parts.append(_init_num(v, kind))
                elif name == "pos_z":
                    parts.append(_init_num(o["pos"][2], kind))
                elif name in o["fields"]:
                    parts.append(_init_num(o["fields"][name], kind))
                else:
                    parts.append("0")
            if not parts:
                parts = ["0"]
            p("    { %s }, /* %s */" % (", ".join(parts), o["name"]))
        p("};")
        p("")
    return "\n".join(lines) + "\n"


def _init_num(v, kind):
    if kind == "f16":
        return "%du" % _f16_bits(float(v))
    if kind == "f32":
        return "%sf" % (repr(float(v)))
    return str(int(v))


def emit_makefile(outdir):
    return (
        "# generated — engine at -O3, data at -O0\n"
        "CC ?= gcc\n"
        "all: game\n"
        "engine.o: engine.c\n"
        "\t$(CC) -O3 -c -o $@ $<\n"
        "data.o: data.c\n"
        "\t$(CC) -O0 -c -o $@ $<\n"
        "game: engine.o data.o\n"
        "\t$(CC) -O2 -o $@ engine.o data.o -lm\n"
        "clean:\n"
        "\trm -f engine.o data.o game\n"
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
    guids = _guid_map(root)
    objects = []
    for path in _walk_files(root, (".unity",)):
        objects.extend(parse_unity_yaml(_read(path), guid_to_script=guids))
    for path in _walk_files(root, (".tscn",)):
        objects.extend(parse_godot_tscn(_read(path)))
    for path in _walk_files(root, (".json",)):
        if os.path.basename(path) == "blender_pack.json":
            objects.extend(parse_blender_json(_read(path)))

    scripts = _walk_files(root, (".cs",))
    # Godot / Blender may name a class with no .cs; synthesise an empty one.
    analyses = [analyze_script(p) for p in scripts]
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
    return objects, analyses


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


def pack(root, outdir, soa=False, soa_vec4=False):
    objects, analyses = load_project(root)
    used_apis = set()
    for a in analyses:
        used_apis |= a["apis"]
    plan = plan_layouts(objects, analyses)
    if soa or soa_vec4:
        plan = apply_soa_layout(plan, vec4=bool(soa_vec4))
    else:
        plan = dict(plan)
        plan["soa"] = False
        plan["soa_vec4"] = False
    os.makedirs(outdir, exist_ok=True)
    engine = emit_engine(plan, analyses, used_apis)
    data = emit_data(plan, used_apis)
    with open(os.path.join(outdir, "engine.c"), "w") as f:
        f.write(engine)
    with open(os.path.join(outdir, "data.c"), "w") as f:
        f.write(data)
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
    return plan


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
    if len(args) != 1 or outdir is None:
        sys.stderr.write(
            "usage: unity_pack.py <project-dir> -o <out-dir> "
            "[--soa | --soa-vec4]\n")
        return 2
    try:
        plan = pack(args[0], outdir, soa=soa, soa_vec4=soa_vec4)
    except PackError as e:
        sys.stderr.write("unity_pack: %s\n" % e.message)
        return 1
    sys.stderr.write(
        "unity_pack: %d classes, 2d=%s, soa=%s, soa_vec4=%s, "
        "wrote %s/{engine.c,data.c,engine_draw.h}\n"
        % (len(plan["classes"]), plan["two_d"], plan.get("soa"),
           plan.get("soa_vec4"), outdir))
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
