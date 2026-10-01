"""unity_pack: AnimationCurve -- a script's curve field, read and evaluated.

A `public AnimationCurve speed;` is serialized in the scene, inside the
MonoBehaviour that holds it: its keys (time, value, in / out slope) and what
happens before the first key and after the last (m_PreInfinity /
m_PostInfinity). It is packed as an index into one table of every curve in
the scene, and `speed.Evaluate(t)` is Unity's evaluation of it:

* between two keys, the cubic Hermite through their values with tangents
  `outSlope * dt` and `inSlope * dt` -- the curve the Inspector draws;
* an infinite slope (a "constant" key) holds the left key's value until the
  next key;
* before the first key and after the last, by the wrap mode: Clamp (Once,
  ClampForever, Default) holds the end value, Loop repeats, PingPong
  mirrors;
* a curve with no keys is 0, one key is that key's value.

Weighted tangents (`weightedMode` other than 0) shape the segment as a Bezier
whose handles the weights stretch, which is another curve; a key using one
is refused at the scene rather than evaluated as if it were not weighted.

Scripts: `curve.Evaluate(t)` and `curve.length`, on a field.
"""
import re

import tools.cs2cpp as cs2cpp

#: Unity's WrapMode values in m_PreInfinity / m_PostInfinity.
WRAP_LOOP = 2
WRAP_PINGPONG = 4


class CurveError(Exception):
    """A curve this pack cannot evaluate as Unity does."""


def _num(v):
    v = v.strip()
    if v in ("Infinity", "-Infinity"):
        return float("inf") if v[0] != "-" else float("-inf")
    return float(v)


def parse_curve(text):
    """The curve serialized in *text* (one field's YAML, its `m_Curve:` and
    wrap modes) -> {"keys": [(time, value, in, out)], "pre", "post"}."""
    keys = []
    km = re.search(r"(?ms)^(\s*)m_Curve:\s*(\[\])?\s*$(.*?)(?=^\1m_\w+:|\Z)", text)
    if km and not km.group(2):
        body = km.group(3)
        for item in re.split(r"(?m)^\s*- serializedVersion:\s*\d+\s*$", body)[1:]:
            def field(name, default=None, item=item):
                m = re.search(r"(?m)^\s*%s:\s*(\S+)\s*$" % name, item)
                if m is None:
                    if default is None:
                        raise CurveError("a key without `%s`" % name)
                    return default
                return m.group(1)
            if int(_num(field("weightedMode", "0"))) != 0:
                raise CurveError(
                    "a key with weighted tangents (weightedMode %s): a weighted "
                    "segment is a Bezier its weights reshape, and this pack "
                    "evaluates Unity's unweighted Hermite only"
                    % field("weightedMode"))
            keys.append((_num(field("time")), _num(field("value")),
                         _num(field("inSlope", "0")), _num(field("outSlope", "0"))))
    keys.sort(key=lambda k: k[0])
    pre = re.search(r"(?m)^\s*m_PreInfinity:\s*(\d+)", text)
    post = re.search(r"(?m)^\s*m_PostInfinity:\s*(\d+)", text)
    return {"keys": keys, "pre": int(pre.group(1)) if pre else 2,
            "post": int(post.group(1)) if post else 2}


def parse_curve_fields(block):
    """{field: curve} for every AnimationCurve a MonoBehaviour block holds:
    a top-level field whose value is a mapping with `m_Curve:`."""
    out = {}
    for m in re.finditer(r"(?m)^  (\w+):\s*\n((?:    .*\n?)+)", block):
        name, value = m.group(1), m.group(2)
        if name.startswith("m_") or not re.search(r"(?m)^    m_Curve:", value):
            continue
        out[name] = parse_curve(value)
    return out


def evaluate(curve, t):
    """Unity's AnimationCurve.Evaluate, in Python (the C below is the same
    arithmetic); the reference the tests compare the engine with."""
    keys = curve["keys"]
    if not keys:
        return 0.0
    if len(keys) == 1:
        return keys[0][1]
    begin, end = keys[0][0], keys[-1][0]
    span = end - begin
    if t < begin or t > end:
        mode = curve["pre"] if t < begin else curve["post"]
        if span <= 0:
            return keys[0][1] if t < begin else keys[-1][1]
        if mode == WRAP_LOOP:
            t = begin + (t - begin) % span
        elif mode == WRAP_PINGPONG:
            u = (t - begin) % (2.0 * span)
            t = begin + (u if u <= span else 2.0 * span - u)
        else:
            return keys[0][1] if t < begin else keys[-1][1]
    i = 0
    while i + 2 < len(keys) and t >= keys[i + 1][0]:
        i += 1
    t0, v0, _in0, out0 = keys[i]
    t1, v1, in1, _out1 = keys[i + 1]
    dt = t1 - t0
    if dt <= 0:
        return v0
    if out0 in (float("inf"), float("-inf")) or in1 in (float("inf"), float("-inf")):
        return v0 if t < t1 else v1
    s = (t - t0) / dt
    s2 = s * s
    s3 = s2 * s
    return ((2 * s3 - 3 * s2 + 1) * v0 + (s3 - 2 * s2 + s) * out0 * dt
            + (s3 - s2) * in1 * dt + (-2 * s3 + 3 * s2) * v1)


def build_table(plan):
    """plan["anim_curves"]: every scene curve; plan["anim_curve_index"]:
    (class, instance, field) -> its row."""
    table, index = [], {}
    for cname in sorted(plan["classes"]):
        for i, o in enumerate(plan["classes"][cname].get("instances") or []):
            for field, curve in sorted((o.get("anim_curves") or {}).items()):
                index[(cname, i, field)] = len(table)
                table.append(curve)
    plan["anim_curves"] = table
    plan["anim_curve_index"] = index


def _c_float(v):
    if v == float("inf"):
        return "1e30f"
    if v == float("-inf"):
        return "-1e30f"
    t = "%.9g" % v
    if "." not in t and "e" not in t and "n" not in t:
        t += ".0"           # `0f` is an integer with a suffix, not C
    return t + "f"


def emit_c(p, plan):
    """The curve table and `AnimationCurve_Evaluate` / `_get_length`."""
    curves = plan.get("anim_curves")
    if curves is None:
        return
    flat, starts = [], []
    for c in curves:
        starts.append(len(flat) // 4)
        for k in c["keys"]:
            flat.extend(k)
    n = max(len(curves), 1)
    p("/* AnimationCurve: every scene curve's keys, (time, value, in, out) */")
    p("static const float _ac_key[%d] = { %s };"
      % (max(len(flat), 1), ", ".join(_c_float(v) for v in flat) or "0"))
    for name, vals in (("_ac_start", starts),
                       ("_ac_n", [len(c["keys"]) for c in curves]),
                       ("_ac_pre", [c["pre"] for c in curves]),
                       ("_ac_post", [c["post"] for c in curves])):
        p("static const int %s[%d] = { %s };"
          % (name, n, ", ".join(str(int(v)) for v in vals) or "0"))
    p("static int AnimationCurve_get_length(int c) {")
    p("    return (c >= 0 && c < %d) ? _ac_n[c] : 0;" % len(curves))
    p("}")
    p("static float AnimationCurve_Evaluate(int c, float t) {")
    p("    const float *k; int n, i; float begin, end, span, t0, t1, dt, s, s2, s3, o0, i1;")
    p("    if (c < 0 || c >= %d) return 0.f;" % len(curves))
    p("    n = _ac_n[c]; k = _ac_key + 4 * _ac_start[c];")
    p("    if (n == 0) return 0.f;")
    p("    if (n == 1) return k[1];")
    p("    begin = k[0]; end = k[4 * (n - 1)]; span = end - begin;")
    p("    if (t < begin || t > end) {")
    p("        int mode = t < begin ? _ac_pre[c] : _ac_post[c];")
    p("        if (span <= 0.f) return t < begin ? k[1] : k[4 * (n - 1) + 1];")
    p("        if (mode == %d) {" % WRAP_LOOP)
    p("            float u = fmodf(t - begin, span); if (u < 0.f) u += span; t = begin + u;")
    p("        } else if (mode == %d) {" % WRAP_PINGPONG)
    p("            float u = fmodf(t - begin, 2.f * span); if (u < 0.f) u += 2.f * span;")
    p("            t = begin + (u <= span ? u : 2.f * span - u);")
    p("        } else {")
    p("            return t < begin ? k[1] : k[4 * (n - 1) + 1];")
    p("        }")
    p("    }")
    p("    i = 0;")
    p("    while (i + 2 < n && t >= k[4 * (i + 1)]) i++;")
    p("    t0 = k[4 * i]; t1 = k[4 * (i + 1)]; dt = t1 - t0;")
    p("    if (dt <= 0.f) return k[4 * i + 1];")
    p("    o0 = k[4 * i + 3]; i1 = k[4 * (i + 1) + 2];")
    p("    if (o0 >= 1e29f || o0 <= -1e29f || i1 >= 1e29f || i1 <= -1e29f)")
    p("        return t < t1 ? k[4 * i + 1] : k[4 * (i + 1) + 1];")
    p("    s = (t - t0) / dt; s2 = s * s; s3 = s2 * s;")
    p("    return (2.f * s3 - 3.f * s2 + 1.f) * k[4 * i + 1] + (s3 - 2.f * s2 + s) * o0 * dt")
    p("         + (s3 - s2) * i1 * dt + (-2.f * s3 + 3.f * s2) * k[4 * (i + 1) + 1];")
    p("}")
    p("")


def lower_api(text, cl, plan, c_ident):
    """`curve.Evaluate(t)` / `curve.length` on an AnimationCurve field."""
    idn = c_ident(cl["name"])
    for f in cl.get("fields") or []:
        if f.get("ty") != "AnimationCurve":
            continue
        recv = r"(?<![\w.])(?:this\s*\.\s*)?%s" % re.escape(f["name"])
        handle = "(int)%s_get_%s(i)" % (idn, f["name"])
        text = cs2cpp.code_sub(
            recv + r"\s*\.\s*Evaluate\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)",
            lambda m, h=handle: "AnimationCurve_Evaluate(%s, (float)(%s))"
            % (h, m.group(1).strip()), text)
        text = cs2cpp.code_sub(recv + r"\s*\.\s*length\b",
                               lambda m, h=handle: "AnimationCurve_get_length(%s)" % h,
                               text)
    return text
