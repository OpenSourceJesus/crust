"""unity_pack: LineRenderer -- read from the scene, scripted, and drawn.

A LineRenderer's points (m_Positions, in world space or its GameObject's),
its width (widthMultiplier times widthCurve) and its color (colorGradient)
are packed into per-line tables. Width and color are Unity's: evaluated at a
point's fraction of the line's length -- the closing segment counts when the
line loops -- the width curve through the AnimationCurve table
(unity_pack_curves.py), the gradient as Unity's Blend (linear between keys,
clamped at the ends) or Fixed (the first key at or after the point) -- and Godot's Constant
(the last key at or before it), for a Line2D's gradient.

It is drawn as the engine draws everything, quads in the draw list: one per
segment, the length of the segment and as wide as the line at its midpoint,
turned to its angle, in the gradient's color at its midpoint (`tex -2`, no
texture). So a line is a chain of straight bands -- Unity's strip with its
joins (numCornerVertices), caps and per-vertex color is approximated, and
the material is not read.

Scripts, on a LineRenderer field (serialized or GetComponent's), local or
`GetComponent<LineRenderer>()`: `positionCount` (get / set), `SetPosition(i,
new Vector3(x, y, z))` / `new Vector2(x, y)`, `GetPosition(i).x` / `.y`,
`loop`, `enabled`, `widthMultiplier` (get / set), `startColor` / `endColor`
(set: the first / last key of the gradient), `startWidth` / `endWidth`
(get).
"""
import re

import tools.unity_pack_curves as _curves

#: At most this many points a line (Unity has no limit; this is the table).
PER_LINE_CAP = 256

#: Unity's named colors (UnityEngine.Color).
NAMED_COLORS = {
    "red": (1.0, 0.0, 0.0, 1.0), "green": (0.0, 1.0, 0.0, 1.0),
    "blue": (0.0, 0.0, 1.0, 1.0), "white": (1.0, 1.0, 1.0, 1.0),
    "black": (0.0, 0.0, 0.0, 1.0), "yellow": (1.0, 0.92156863, 0.015686275, 1.0),
    "cyan": (0.0, 1.0, 1.0, 1.0), "magenta": (1.0, 0.0, 1.0, 1.0),
    "gray": (0.5, 0.5, 0.5, 1.0), "grey": (0.5, 0.5, 0.5, 1.0),
    "clear": (0.0, 0.0, 0.0, 0.0),
}


class LineError(Exception):
    """A LineRenderer this pack cannot draw as Unity does."""


def _sub_block(text, key):
    """The mapping under `key:` (deeper-indented lines after it)."""
    m = re.search(r"(?m)^(\s*)%s:\s*\n" % re.escape(key), text)
    if m is None:
        return ""
    ind = len(m.group(1))
    out = []
    for line in text[m.end():].split("\n"):
        if line.strip() and len(line) - len(line.lstrip()) <= ind:
            break
        out.append(line)
    return "\n".join(out)


def _num(text, key, default):
    m = re.search(r"(?m)^\s*%s:\s*([-0-9.eE+]+)\s*$" % re.escape(key), text)
    return float(m.group(1)) if m else float(default)


def parse_gradient(text):
    """A serialized Gradient -> {"mode", "colors": [(t, r, g, b)],
    "alphas": [(t, a)]}, times in 0..1."""
    mode = int(_num(text, "m_Mode", 0))
    if mode not in (0, 1):
        raise LineError("a gradient in mode %d (PerceptualBlend): it blends in "
                        "another color space, and this pack draws Blend and "
                        "Fixed only" % mode)
    nc = int(_num(text, "m_NumColorKeys", 2))
    na = int(_num(text, "m_NumAlphaKeys", 2))
    keys = {}
    for km in re.finditer(r"(?m)^\s*key(\d):\s*\{r:\s*([-0-9.eE+]+),\s*g:\s*"
                          r"([-0-9.eE+]+),\s*b:\s*([-0-9.eE+]+),\s*a:\s*([-0-9.eE+]+)\}",
                          text):
        keys[int(km.group(1))] = tuple(float(km.group(k)) for k in (2, 3, 4, 5))
    white = (1.0, 1.0, 1.0, 1.0)
    colors = [(_num(text, "ctime%d" % i, 0 if i == 0 else 65535) / 65535.0,)
              + keys.get(i, white)[:3] for i in range(max(nc, 1))]
    alphas = [(_num(text, "atime%d" % i, 0 if i == 0 else 65535) / 65535.0,
               keys.get(i, white)[3]) for i in range(max(na, 1))]
    return {"mode": mode, "colors": sorted(colors), "alphas": sorted(alphas)}


def parse_line_renderer(block):
    """A LineRenderer component's YAML -> its record."""
    pos = []
    pm = re.search(r"(?m)^  m_Positions:\s*(\[\])?\s*$", block)
    if pm and not pm.group(1):
        for vm in re.finditer(r"(?m)^  - \{x:\s*([-0-9.eE+]+),\s*y:\s*([-0-9.eE+]+),"
                              r"\s*z:\s*([-0-9.eE+]+)\}", block[pm.end():]):
            pos.append((float(vm.group(1)), float(vm.group(2))))
            # stop at the next top-level key
        nxt = re.search(r"(?m)^  m_\w+:", block[pm.end():])
        if nxt:
            kept = len(re.findall(r"(?m)^  - \{x:", block[pm.end():pm.end() + nxt.start()]))
            pos = pos[:kept]
    if len(pos) > PER_LINE_CAP:
        raise LineError("%d points (this pack's table holds %d a line)"
                        % (len(pos), PER_LINE_CAP))
    params = _sub_block(block, "m_Parameters")
    wc = _sub_block(params, "widthCurve")
    width = _curves.parse_curve(wc) if wc.strip() else \
        {"keys": [(0.0, 1.0, 0.0, 0.0)], "pre": 2, "post": 2}
    grad = _sub_block(params, "colorGradient")
    return {
        "enabled": int(_num(block, "m_Enabled", 1)),
        "positions": pos,
        "world": int(_num(block, "m_UseWorldSpace", 1)),
        "loop": int(_num(block, "m_Loop", 0)),
        "mult": _num(params, "widthMultiplier", 1.0),
        "width": width,
        "gradient": parse_gradient(grad) if grad.strip() else
        {"mode": 0, "colors": [(0.0, 1.0, 1.0, 1.0), (1.0, 1.0, 1.0, 1.0)],
         "alphas": [(0.0, 1.0), (1.0, 1.0)]},
        "sorting_order": int(_num(block, "m_SortingOrder", 0)),
    }


def gradient_eval(g, t):
    """Unity's Gradient.Evaluate (Blend / Fixed), the reference for the C."""
    def lookup(keys, t):
        if g["mode"] == 2:                    # Godot's Constant: at or before
            v = keys[0][1:]
            for k in keys:
                if k[0] <= t:
                    v = k[1:]
            return v
        if g["mode"] == 1:                                   # Fixed
            for k in keys:
                if k[0] >= t:
                    return k[1:]
            return keys[-1][1:]
        if t <= keys[0][0]:
            return keys[0][1:]
        for a, b in zip(keys, keys[1:]):
            if t <= b[0]:
                u = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
                return tuple(x + (y - x) * u for x, y in zip(a[1:], b[1:]))
        return keys[-1][1:]
    r, gg, b = lookup(g["colors"], t)
    (a,) = lookup(g["alphas"], t)
    return (r, gg, b, a)


def build_table(plan):
    """plan["lines"]: each scene line, its owner and its width curve's row in
    the curve table (built first); plan["lr_by_file_id"]."""
    lines, by_fid = [], {}
    curves = plan.setdefault("anim_curves", [])
    for cname in sorted(plan["classes"]):
        for i, o in enumerate(plan["classes"][cname].get("instances") or []):
            lr = o.get("line_renderer")
            if not lr:
                continue
            if not lr["world"] and not lr.get("baked_xform"):
                q = o.get("rot") or (0.0, 0.0, 0.0, 1.0)
                sc = o.get("local_scale") or (1.0, 1.0, 1.0)
                if any(abs(a - b) > 1e-6 for a, b in zip(list(q) + [1.0] * 4,
                                                         (0.0, 0.0, 0.0, 1.0))) \
                        or any(abs(float(v) - 1.0) > 1e-6 for v in sc):
                    raise LineError(
                        "GameObject `%s`: a LineRenderer in local space on a "
                        "rotated or scaled object -- its points are drawn "
                        "offset by the object's position only, so this is not "
                        "packed yet; use world space" % o.get("name"))
            if lr.get("file_id") is not None:
                by_fid[str(lr["file_id"])] = len(lines)
            row = len(curves)
            curves.append(lr["width"])
            pos = o.get("pos") or (0.0, 0.0, 0.0)
            lines.append(dict(lr, owner_class=cname, owner_inst=i,
                              go_index=o.get("go_index"), wcurve=row,
                              pos=(float(pos[0]), float(pos[1]))))
    plan["lines"] = lines
    plan["lr_by_file_id"] = by_fid


def _f(v):
    t = "%.9g" % float(v)
    if "." not in t and "e" not in t and "n" not in t:
        t += ".0"
    return t + "f"


def emit_api(p, plan):
    """The tables and the scripts' API (after the curve table)."""
    lines = plan.get("lines") or []
    if not lines:
        return
    n, cap = len(lines), PER_LINE_CAP

    def arr(cty, name, vals, const=True):
        p("static %s%s _lr_%s[%d] = { %s };" % ("const " if const else "", cty, name,
                                                len(vals), ", ".join(vals)))
    p("/* LineRenderers (tools/unity_pack_lines.py) */")
    arr("int", "go", [str(int(l["go_index"])) if l.get("go_index") is not None else "-1"
                      for l in lines])
    arr("int", "world", [str(int(l["world"])) for l in lines])
    arr("int", "wcurve", [str(l["wcurve"]) for l in lines])
    arr("int", "order", [str(int(l["sorting_order"])) for l in lines])
    arr("int", "n", [str(len(l["positions"])) for l in lines], const=False)
    arr("int", "loop", [str(int(l["loop"])) for l in lines], const=False)
    arr("int", "enabled", [str(int(l["enabled"])) for l in lines], const=False)
    arr("float", "mult", [_f(l["mult"]) for l in lines], const=False)
    px, py = [], []
    for l in lines:
        pts = list(l["positions"]) + [(0.0, 0.0)] * (cap - len(l["positions"]))
        px += [_f(x) for x, _y in pts]
        py += [_f(y) for _x, y in pts]
    arr("float", "x", px, const=False)
    arr("float", "y", py, const=False)
    # gradients: up to 8 color and 8 alpha keys a line (Unity's limit)
    gm, cn, an, ct, cr, cg, cb, at, aa = [], [], [], [], [], [], [], [], []
    for l in lines:
        g = l["gradient"]
        gm.append(str(g["mode"]))
        cs = (g["colors"] + [g["colors"][-1]] * 8)[:8]
        al = (g["alphas"] + [g["alphas"][-1]] * 8)[:8]
        cn.append(str(min(len(g["colors"]), 8)))
        an.append(str(min(len(g["alphas"]), 8)))
        for c in cs:
            ct.append(_f(c[0])); cr.append(_f(c[1])); cg.append(_f(c[2])); cb.append(_f(c[3]))
        for a in al:
            at.append(_f(a[0])); aa.append(_f(a[1]))
    arr("int", "gmode", gm)
    arr("int", "cn", cn)
    arr("int", "an", an)
    for name, vals in (("ct", ct), ("cr", cr), ("cg", cg), ("cb", cb),
                       ("at", at), ("aa", aa)):
        arr("float", name, vals, const=False)
    p("static int _lr_ok(int s) { return s >= 0 && s < %d; }" % n)
    p("static float _lr_glookup(int mode, const float *t, const float *v, int k, float u) {")
    p("    int j;")
    p("    if (mode == 1) { for (j = 0; j < k; j++) if (t[j] >= u) return v[j]; return v[k - 1]; }")
    p("    if (mode == 2) { float r = v[0]; for (j = 0; j < k; j++) if (t[j] <= u) r = v[j]; return r; }")
    p("    if (u <= t[0]) return v[0];")
    p("    for (j = 0; j + 1 < k; j++) if (u <= t[j + 1]) {")
    p("        float d = t[j + 1] - t[j];")
    p("        return d > 0.f ? v[j] + (v[j + 1] - v[j]) * ((u - t[j]) / d) : v[j];")
    p("    }")
    p("    return v[k - 1];")
    p("}")
    p("static void _lr_color(int s, float u, float *r, float *g, float *b, float *a) {")
    p("    int o = 8 * s, m = _lr_gmode[s];")
    p("    *r = _lr_glookup(m, _lr_ct + o, _lr_cr + o, _lr_cn[s], u);")
    p("    *g = _lr_glookup(m, _lr_ct + o, _lr_cg + o, _lr_cn[s], u);")
    p("    *b = _lr_glookup(m, _lr_ct + o, _lr_cb + o, _lr_cn[s], u);")
    p("    *a = _lr_glookup(m, _lr_at + o, _lr_aa + o, _lr_an[s], u);")
    p("}")
    p("static float _lr_width(int s, float u) {")
    p("    return _lr_mult[s] * AnimationCurve_Evaluate(_lr_wcurve[s], u);")
    p("}")
    p("static int LineRenderer_get_positionCount(int s) { return _lr_ok(s) ? _lr_n[s] : 0; }")
    p("static void LineRenderer_set_positionCount(int s, int c) {")
    p("    int k;")
    p("    if (!_lr_ok(s)) return;")
    p("    if (c < 0) c = 0;")
    p("    if (c > %d) c = %d;" % (cap, cap))
    p("    for (k = _lr_n[s]; k < c; k++) { _lr_x[%d * s + k] = 0.f; _lr_y[%d * s + k] = 0.f; }" % (cap, cap))
    p("    _lr_n[s] = c;")
    p("}")
    p("static void LineRenderer_SetPosition(int s, int i, float x, float y) {")
    p("    if (!_lr_ok(s) || i < 0 || i >= _lr_n[s]) return;")
    p("    _lr_x[%d * s + i] = x; _lr_y[%d * s + i] = y;" % (cap, cap))
    p("}")
    p("static float LineRenderer_GetPosition_x(int s, int i) {")
    p("    return (_lr_ok(s) && i >= 0 && i < _lr_n[s]) ? _lr_x[%d * s + i] : 0.f;" % cap)
    p("}")
    p("static float LineRenderer_GetPosition_y(int s, int i) {")
    p("    return (_lr_ok(s) && i >= 0 && i < _lr_n[s]) ? _lr_y[%d * s + i] : 0.f;" % cap)
    p("}")
    for prop, cty in (("loop", "int"), ("enabled", "int"), ("mult", "float")):
        name = "widthMultiplier" if prop == "mult" else prop
        p("static %s LineRenderer_get_%s(int s) { return _lr_ok(s) ? _lr_%s[s] : 0; }"
          % (cty, name, prop))
        p("static void LineRenderer_set_%s(int s, %s v) { if (_lr_ok(s)) _lr_%s[s] = v; }"
          % (name, cty, prop))
    p("static float LineRenderer_get_startWidth(int s) { return _lr_ok(s) ? _lr_width(s, 0.f) : 0.f; }")
    p("static float LineRenderer_get_endWidth(int s) { return _lr_ok(s) ? _lr_width(s, 1.f) : 0.f; }")
    p("/* startColor / endColor: the first / last key of the gradient */")
    p("static void LineRenderer_set_startColor(int s, float r, float g, float b, float a) {")
    p("    if (!_lr_ok(s)) return;")
    p("    _lr_cr[8 * s] = r; _lr_cg[8 * s] = g; _lr_cb[8 * s] = b; _lr_aa[8 * s] = a;")
    p("}")
    p("static void LineRenderer_set_endColor(int s, float r, float g, float b, float a) {")
    p("    int c, k;")
    p("    if (!_lr_ok(s)) return;")
    p("    c = 8 * s + _lr_cn[s] - 1; k = 8 * s + _lr_an[s] - 1;")
    p("    _lr_cr[c] = r; _lr_cg[c] = g; _lr_cb[c] = b; _lr_aa[k] = a;")
    p("}")
    p("static int GameObject_GetComponent_LineRenderer(int go) {")
    p("    int s;")
    p("    for (s = 0; s < %d; s = s + 1) if (_lr_go[s] == go) return s;" % n)
    p("    return -1;")
    p("}")
    p("static void _lr_owner_pos(int s, float *x, float *y);")
    p("")


def emit_owner_pos(p, plan, c_ident, class_has_position):
    """After the class code: a local-space line follows its object."""
    lines = plan.get("lines") or []
    if not lines:
        return
    p("static void _lr_owner_pos(int s, float *x, float *y) {")
    p("    switch (s) {")
    for k, l in enumerate(lines):
        cl = plan["classes"].get(l["owner_class"]) or {}
        if class_has_position(cl) and not cl.get("static"):
            idn = c_ident(l["owner_class"])
            p("    case %d: *x = %s_get_pos_x(%du); *y = %s_get_pos_y(%du); return;"
              % (k, idn, l["owner_inst"], idn, l["owner_inst"]))
        else:
            p("    case %d: *x = %s; *y = %s; return;" % (k, _f(l["pos"][0]), _f(l["pos"][1])))
    p("    default: *x = 0.f; *y = 0.f; return;")
    p("    }")
    p("}")
    p("")


def emit_collect(p, plan):
    """The draw list's lines: one quad a segment."""
    lines = plan.get("lines") or []
    if not lines:
        return
    cap = PER_LINE_CAP
    p("static void _lr_collect(EngineDraw *out, int *n, int max) {")
    p("    int s, k, segs;")
    p("    for (s = 0; s < %d; s = s + 1) {" % len(lines))
    p("        float ox = 0.f, oy = 0.f, total = 0.f, run = 0.f;")
    p("        int c = _lr_n[s];")
    p("        if (!_lr_enabled[s] || c < 2) continue;")
    p("        if (!_lr_world[s]) _lr_owner_pos(s, &ox, &oy);")
    p("        segs = _lr_loop[s] ? c : c - 1;")
    p("        for (k = 0; k < segs; k = k + 1) {")
    p("            int a = %d * s + k, b = %d * s + (k + 1) %% c;" % (cap, cap))
    p("            float dx = _lr_x[b] - _lr_x[a], dy = _lr_y[b] - _lr_y[a];")
    p("            total += sqrtf(dx * dx + dy * dy);")
    p("        }")
    p("        for (k = 0; k < segs && *n < max; k = k + 1) {")
    p("            int a = %d * s + k, b = %d * s + (k + 1) %% c;" % (cap, cap))
    p("            float dx = _lr_x[b] - _lr_x[a], dy = _lr_y[b] - _lr_y[a];")
    p("            float len = sqrtf(dx * dx + dy * dy), u, w, cs, sn;")
    p("            EngineDraw *d = &out[*n];")
    p("            u = total > 0.f ? (run + 0.5f * len) / total : 0.f;")
    p("            run += len;")
    p("            if (len <= 0.f) continue;")
    p("            w = _lr_width(s, u);")
    p("            cs = dx / len; sn = dy / len;")
    p("            d->x = ox + 0.5f * (_lr_x[a] + _lr_x[b]);")
    p("            d->y = oy + 0.5f * (_lr_y[a] + _lr_y[b]);")
    p("            d->half_w = 0.5f * len;")
    p("            d->half_h = 0.5f * w;")
    p("            d->m00 = cs; d->m01 = -sn; d->m10 = sn; d->m11 = cs;")
    p("            _lr_color(s, u, &d->r, &d->g, &d->b, &d->a);")
    p("            d->tex = -2; /* no texture: its color */")
    p("            d->sorting_layer = 0;")
    p("            d->sorting_order = _lr_order[s];")
    p("            d->flags = 0;")
    p("            d->go = _lr_go[s];")
    p("            *n = *n + 1;")
    p("        }")
    p("    }")
    p("}")
    p("")


_VEC_ARG = r"new\s+(?:UnityEngine\s*\.\s*)?Vector[23]\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)"
_COLOR_ARG = r"new\s+(?:UnityEngine\s*\.\s*)?Color\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)"


def _split_args(s):
    parts, depth, cur = [], 0, []
    for c in s:
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur).strip())
    return parts


def _color_args(text):
    """`new Color(r, g, b[, a])` / `Color.red` -> 'r, g, b, a' or None."""
    m = re.match(r"^\s*%s\s*$" % _COLOR_ARG, text)
    if m:
        a = _split_args(m.group(1))
        if len(a) in (3, 4):
            a = a + ["1"] if len(a) == 3 else a
            return ", ".join("(float)(%s)" % x for x in a)
        return None
    m = re.match(r"^\s*(?:UnityEngine\s*\.\s*)?Color\s*\.\s*(\w+)\s*$", text)
    if m and m.group(1) in NAMED_COLORS:
        return ", ".join(_f(v) for v in NAMED_COLORS[m.group(1)])
    return None


def lower_api(text, cl, plan, c_ident):
    """LineRenderer members on a field, local or GetComponent's."""
    if not plan.get("lines"):
        return text
    import tools.cs2cpp as cs2cpp
    idn = c_ident(cl["name"])
    call = "GameObject_GetComponent_LineRenderer(_engine_go_of_%s(i))" % idn
    text = cs2cpp.code_sub(
        r"(?<![\w.])(?:this\s*\.\s*)?(?:gameObject\s*\.\s*)?GetComponent\s*<\s*"
        r"(?:UnityEngine\s*\.\s*)?LineRenderer\s*>\s*\(\s*\)", call, text)
    recvs = {}
    for f in cl.get("fields") or []:
        if f.get("ty") == "LineRenderer":
            recvs[r"(?<![\w.])(?:this\s*\.\s*)?%s" % re.escape(f["name"])] = \
                "(int)%s_get_%s(i)" % (idn, f["name"])
    for nm in set(re.findall(r"(?<![\w.<])(?:UnityEngine\s*\.\s*)?LineRenderer\s+(\w+)"
                             r"(?=\s*[=;,)])", cs2cpp._blank(text))):
        recvs[r"(?<![\w.])%s" % re.escape(nm)] = nm
    recvs[re.escape(call)] = call
    text = cs2cpp.code_sub(r"(?<![\w.<])(?:UnityEngine\s*\.\s*)?LineRenderer(?=\s+\w+\s*[=;])",
                           "int", text)
    for pat, rx in recvs.items():
        text = cs2cpp.code_sub(pat + r"\s*(==|!=)\s*null\b",
                               lambda m, rx=rx: "(%s %s)" % (
                                   rx, "< 0" if m.group(1) == "==" else ">= 0"), text)
        text = cs2cpp.code_sub(
            pat + r"\s*\.\s*SetPosition\s*\(([^,()]*(?:\([^()]*\)[^,()]*)*),\s*" + _VEC_ARG + r"\s*\)",
            lambda m, rx=rx: "LineRenderer_SetPosition(%s, (int)(%s), %s)" % (
                rx, m.group(1).strip(),
                ", ".join("(float)(%s)" % a for a in _split_args(m.group(2))[:2])), text)
        text = cs2cpp.code_sub(
            pat + r"\s*\.\s*GetPosition\s*\(([^()]*)\)\s*\.\s*([xy])\b",
            lambda m, rx=rx: "LineRenderer_GetPosition_%s(%s, (int)(%s))" % (
                m.group(2), rx, m.group(1).strip()), text)
        for prop in ("startColor", "endColor"):
            def color_set(m, rx=rx, prop=prop):
                args = _color_args(m.group(1))
                if args is None:
                    return m.group(0)       # left: the stub check reports it
                return "LineRenderer_set_%s(%s, %s);" % (prop, rx, args)
            text = cs2cpp.code_sub(pat + r"\s*\.\s*%s\s*=(?!=)\s*([^;]+);" % prop,
                                   color_set, text)
        for prop in ("positionCount", "loop", "enabled", "widthMultiplier"):
            text = cs2cpp.code_sub(
                pat + r"\s*\.\s*%s\s*=(?!=)\s*([^;]+);" % prop,
                lambda m, rx=rx, prop=prop: "LineRenderer_set_%s(%s, %s);" % (
                    prop, rx, m.group(1).strip()), text)
        for prop in ("positionCount", "loop", "enabled", "widthMultiplier",
                     "startWidth", "endWidth"):
            text = cs2cpp.code_sub(pat + r"\s*\.\s*%s\b(?!\s*=[^=])" % prop,
                                   lambda m, rx=rx, prop=prop: "LineRenderer_get_%s(%s)" % (
                                       prop, rx), text)
    return text
