"""unity_pack: ParticleSystem -- the component read, simulated and drawn.

The scene's ParticleSystem (its main, emission and shape modules) is a system
of the engine's particle tables: it emits over time (rateOverTime) and in its
bursts while it plays, a particle leaving the emitter's shape (a cone along
the emitter's +Z, as its authored rotation turns it -- a default 2D system
points up; a sphere / circle all around) at its start speed, with its start
lifetime, size and color (a constant, or a random pick between two), falling
by gravityModifier; a system simulated in local space follows its emitter.
They are simulated after LateUpdate, where Unity simulates them, and drawn
as squares of their color (the draw list's `tex -2`: no texture). The curve
modes of a MinMaxCurve / Gradient read their scalar / max color; the other
modules (size / color over lifetime, noise, collision, sub-emitters,
trails) and the renderer's material are not read.

Scripts: a ParticleSystem field (serialized or GetComponent's), local or
`GetComponent<ParticleSystem>()` -- Play(), Stop() (stops emitting; its
particles live on, Unity's default), Pause(), Clear(), Emit(n), isPlaying,
isEmitting, isPaused, isStopped, particleCount.
"""
import math
import re

import tools.cs2cpp as cs2cpp

#: At most this many live particles a system (and maxNumParticles).
PER_SYSTEM_CAP = 512


def _curve(block, key, default):
    """A MinMaxCurve: (min, max) -- a constant twice, or two constants; a
    curve's scalar."""
    m = re.search(r"(?m)^(\s+)%s:\s*\n((?:\1  .*\n?)+)" % re.escape(key), block)
    if not m:
        return (default, default)
    body = m.group(2)

    def num(k, d):
        mm = re.search(r"(?m)^\s+%s:\s*([-0-9.eE+]+)" % k, body)
        return float(mm.group(1)) if mm else d
    state = int(num("minMaxState", 0))
    hi = num("scalar", default)
    lo = num("minScalar", hi) if state == 3 else hi
    return (lo, hi)


def _color(block, key):
    m = re.search(r"(?m)^(\s+)%s:\s*\n((?:\1  .*\n?)+)" % re.escape(key), block)
    white = (1.0, 1.0, 1.0, 1.0)
    if not m:
        return (white, white)
    body = m.group(2)

    def col(k):
        cm = re.search(r"%s:\s*\{r:\s*([^,]+),\s*g:\s*([^,]+),\s*b:\s*([^,]+),\s*a:\s*([^}]+)\}"
                       % k, body)
        return tuple(float(cm.group(i)) for i in range(1, 5)) if cm else white
    state = re.search(r"(?m)^\s+minMaxState:\s*(\d+)", body)
    hi = col("maxColor")
    lo = col("minColor") if state and state.group(1) == "2" else hi
    return (lo, hi)


def _module(block, name):
    m = re.search(r"(?ms)^  %s:\s*\n(.*?)(?=^  \w+:\s*$|^  \w+Module:|\Z)" % name, block)
    return m.group(1) if m else ""


def parse_particle_system(block):
    """A ParticleSystem component's YAML -> its record."""
    def num(key, default, text=block):
        m = re.search(r"(?m)^\s+%s:\s*([-0-9.eE+]+)\s*$" % key, text)
        return float(m.group(1)) if m else float(default)
    init = _module(block, "InitialModule")
    emit = _module(block, "EmissionModule")
    shape = _module(block, "ShapeModule")
    bursts = []
    for bm in re.finditer(r"(?ms)^\s+- serializedVersion: \d+\s*\n\s+time:\s*([-0-9.eE+]+)"
                          r"(.*?)(?=^\s+- serializedVersion|\Z)", emit):
        cnt = _curve(bm.group(2), "countCurve", 30.0)
        bursts.append((float(bm.group(1)), cnt[1]))
    rad = re.search(r"(?m)^    radius:\s*\n\s+value:\s*([-0-9.eE+]+)", shape)
    return {
        "duration": num("lengthInSec", 5.0),
        "looping": int(num("looping", 1)),
        "play_on_awake": int(num("playOnAwake", 1)),
        "sim_speed": num("simulationSpeed", 1.0),
        # moveWithTransform: 0 Local (particles follow the emitter), 1 World
        "world": int(num("moveWithTransform", 0)) == 1,
        "lifetime": _curve(init, "startLifetime", 5.0),
        "speed": _curve(init, "startSpeed", 5.0),
        "size": _curve(init, "startSize", 1.0),
        "gravity": _curve(init, "gravityModifier", 0.0)[1],
        "color": _color(init, "startColor"),
        "max": int(num("maxNumParticles", 1000, init)),
        "rate": _curve(emit, "rateOverTime", 10.0)[1]
        if int(num("enabled", 1, emit)) else 0.0,
        "bursts": bursts,
        # ParticleSystemShapeType: 0 sphere, 2 hemisphere, 4 cone, 10 circle
        "shape": int(num("type", 4, shape)) if int(num("enabled", 1, shape)) else -1,
        "angle": num("angle", 25.0, shape),
        "radius": float(rad.group(1)) if rad else num("radius", 1.0, shape),
    }


def build_particle_table(plan):
    """plan["particles"]: each authored system, with its owner, GameObject
    and emitter axis (the local +Z its authored rotation turns, in 2D)."""
    systems, by_fid = [], {}
    for cname in sorted(plan["classes"]):
        for i, o in enumerate(plan["classes"][cname].get("instances") or []):
            ps = o.get("particle_system")
            if not ps:
                continue
            q = o.get("rot") or (0.0, 0.0, 0.0, 1.0)
            x, y, z, w = (list(q) + [1.0] * 4)[:4]
            # q * (0, 0, 1)
            ax = 2.0 * (x * z + w * y)
            ay = 2.0 * (y * z - w * x)
            n = math.hypot(ax, ay)
            axis = (ax / n, ay / n) if n > 1e-4 else (0.0, 1.0)
            pos = o.get("pos") or (0.0, 0.0, 0.0)
            if ps.get("file_id") is not None:
                by_fid[str(ps["file_id"])] = len(systems)
            systems.append(dict(ps, owner_class=cname, owner_inst=i,
                                go_index=o.get("go_index"), axis=axis,
                                pos=(float(pos[0]), float(pos[1]))))
    plan["particles"] = systems
    plan["ps_by_file_id"] = by_fid


def emit_api(p, plan):
    """The tables and the scripts' API: before the class code."""
    ps = plan.get("particles") or []
    if not ps:
        return
    n = len(ps)
    cap = [max(1, min(int(s["max"]), PER_SYSTEM_CAP)) for s in ps]
    base = [sum(cap[:k]) for k in range(n)]
    total = sum(cap)

    def arr(cty, name, vals):
        p("static const %s _ps_%s[%d] = { %s };" % (cty, name, n, ", ".join(vals)))

    def f(v):
        return "%sf" % repr(float(v))
    p("/* ParticleSystems (tools/unity_pack_particles.py) */")
    arr("int", "go", [str(int(s["go_index"]) if s.get("go_index") is not None else -1)
                      for s in ps])
    arr("int", "base", [str(b) for b in base])
    arr("int", "cap", [str(c) for c in cap])
    arr("int", "looping", [str(int(s["looping"])) for s in ps])
    arr("int", "world", [str(int(bool(s["world"]))) for s in ps])
    arr("int", "shape", [str(int(s["shape"])) for s in ps])
    for name, key in (("duration", "duration"), ("sim", "sim_speed"),
                      ("gravity", "gravity"), ("rate", "rate"), ("angle", "angle"),
                      ("radius", "radius")):
        arr("float", name, [f(s[key]) for s in ps])
    for name, key in (("life", "lifetime"), ("speed", "speed"), ("size", "size")):
        arr("float", name + "0", [f(s[key][0]) for s in ps])
        arr("float", name + "1", [f(s[key][1]) for s in ps])
    for k, ch in enumerate("rgba"):
        arr("float", "c0" + ch, [f(s["color"][0][k]) for s in ps])
        arr("float", "c1" + ch, [f(s["color"][1][k]) for s in ps])
    arr("float", "axis_x", [f(s["axis"][0]) for s in ps])
    arr("float", "axis_y", [f(s["axis"][1]) for s in ps])
    nb = sum(len(s["bursts"]) for s in ps)
    bstart = [sum(len(t["bursts"]) for t in ps[:k]) for k in range(n)]
    arr("int", "burst_start", [str(b) for b in bstart])
    arr("int", "burst_count", [str(len(s["bursts"])) for s in ps])
    p("static const float _ps_burst_t[%d] = { %s };" % (max(1, nb), ", ".join(
        f(t) for s in ps for t, _c in s["bursts"]) or "0.f"))
    p("static const float _ps_burst_n[%d] = { %s };" % (max(1, nb), ", ".join(
        f(c) for s in ps for _t, c in s["bursts"]) or "0.f"))
    p("/* state: playing, emitting, paused; time into the cycle; emission owed */")
    p("static unsigned char _ps_playing[%d] = { %s };" % (n, ", ".join(
        str(int(s["play_on_awake"])) for s in ps)))
    p("static unsigned char _ps_emitting[%d] = { %s };" % (n, ", ".join(
        str(int(s["play_on_awake"])) for s in ps)))
    p("static unsigned char _ps_paused[%d];" % n)
    p("static float _ps_time[%d], _ps_owed[%d];" % (n, n))
    p("static int _ps_alive[%d];" % n)
    p("/* the particles: position, velocity, age, lifetime, size, color */")
    for col in ("x", "y", "vx", "vy", "age", "life", "size", "r", "g", "b", "a"):
        p("static float _ps_p_%s[%d];" % (col, total))
    p("static unsigned _ps_seed = 12345u;")
    p("static float _ps_rand(void) {")
    p("    _ps_seed = _ps_seed * 1664525u + 1013904223u;")
    p("    return (float)((_ps_seed >> 8) & 0xFFFFFFu) / 16777216.f;")
    p("}")
    p("static int _ps_ok(int s) { return s >= 0 && s < %d; }" % n)
    p("static void _ps_emit(int s, int count);")
    p("static void ParticleSystem_Play(int s) {")
    p("    if (!_ps_ok(s)) return;")
    p("    if (!_ps_playing[s] || !_ps_emitting[s]) { _ps_time[s] = 0.f; _ps_owed[s] = 0.f; }")
    p("    _ps_playing[s] = 1; _ps_emitting[s] = 1; _ps_paused[s] = 0;")
    p("}")
    p("static void ParticleSystem_Stop(int s) {")
    p("    if (_ps_ok(s)) _ps_emitting[s] = 0; /* StopEmitting: they live on */")
    p("}")
    p("static void ParticleSystem_Pause(int s) { if (_ps_ok(s)) _ps_paused[s] = 1; }")
    p("static void ParticleSystem_Clear(int s) { if (_ps_ok(s)) _ps_alive[s] = 0; }")
    p("static void ParticleSystem_Emit(int s, int count) { if (_ps_ok(s)) _ps_emit(s, count); }")
    p("static int ParticleSystem_get_isPlaying(int s) {")
    p("    return _ps_ok(s) && !_ps_paused[s] && (_ps_emitting[s] || _ps_alive[s] > 0);")
    p("}")
    p("static int ParticleSystem_get_isEmitting(int s) {")
    p("    return _ps_ok(s) && _ps_emitting[s] && !_ps_paused[s];")
    p("}")
    p("static int ParticleSystem_get_isPaused(int s) { return _ps_ok(s) && _ps_paused[s]; }")
    p("static int ParticleSystem_get_isStopped(int s) {")
    p("    return _ps_ok(s) && !_ps_emitting[s] && _ps_alive[s] == 0;")
    p("}")
    p("static int ParticleSystem_get_particleCount(int s) { return _ps_ok(s) ? _ps_alive[s] : 0; }")
    p("static int GameObject_GetComponent_ParticleSystem(int go) {")
    p("    int s;")
    p("    for (s = 0; s < %d; s = s + 1) if (_ps_go[s] == go) return s;" % n)
    p("    return -1;")
    p("}")
    p("static void _ps_emitter_pos(int s, float *x, float *y);")
    p("")


def emit_sim(p, plan, class_ids, c_ident, class_has_position):
    """The simulation (after the class code: it reads the emitters' Transform)."""
    ps = plan.get("particles") or []
    if not ps:
        return
    p("static void _ps_emitter_pos(int s, float *x, float *y) {")
    p("    switch (s) {")
    for k, s in enumerate(ps):
        cl = plan["classes"].get(s["owner_class"]) or {}
        if class_has_position(cl) and not cl.get("static"):
            idn = c_ident(s["owner_class"])
            p("    case %d: *x = %s_get_pos_x(%du); *y = %s_get_pos_y(%du); return;"
              % (k, idn, s["owner_inst"], idn, s["owner_inst"]))
        else:
            p("    case %d: *x = %sf; *y = %sf; return;" % (k, repr(s["pos"][0]), repr(s["pos"][1])))
    p("    default: *x = 0.f; *y = 0.f; return;")
    p("    }")
    p("}")
    p("/* One particle from the emitter's shape: a cone along its axis, or all")
    p("   around (a sphere / circle); a local system's are relative to it. */")
    p("static void _ps_emit(int s, int count) {")
    p("    int k;")
    p("    float ex = 0.f, ey = 0.f;")
    p("    if (_ps_world[s]) _ps_emitter_pos(s, &ex, &ey);")
    p("    for (k = 0; k < count && _ps_alive[s] < _ps_cap[s]; k = k + 1) {")
    p("        int j = _ps_base[s] + _ps_alive[s];")
    p("        float dx, dy, ox = 0.f, oy = 0.f, spd, t;")
    p("        if (_ps_shape[s] == 4) {")
    p("            float a = (_ps_rand() * 2.f - 1.f) * _ps_angle[s] * 0.0174532925f;")
    p("            float c = cosf(a), sn = sinf(a), off = (_ps_rand() * 2.f - 1.f) * _ps_radius[s];")
    p("            dx = _ps_axis_x[s] * c - _ps_axis_y[s] * sn;")
    p("            dy = _ps_axis_x[s] * sn + _ps_axis_y[s] * c;")
    p("            ox = -_ps_axis_y[s] * off;")
    p("            oy = _ps_axis_x[s] * off;")
    p("        } else if (_ps_shape[s] >= 0) {")
    p("            float a = _ps_rand() * 6.2831853f, r = _ps_rand() * _ps_radius[s];")
    p("            dx = cosf(a); dy = sinf(a);")
    p("            ox = dx * r; oy = dy * r;")
    p("        } else {")
    p("            dx = _ps_axis_x[s]; dy = _ps_axis_y[s];")
    p("        }")
    p("        t = _ps_rand();")
    p("        spd = _ps_speed0[s] + (_ps_speed1[s] - _ps_speed0[s]) * t;")
    p("        _ps_p_x[j] = ex + ox; _ps_p_y[j] = ey + oy;")
    p("        _ps_p_vx[j] = dx * spd; _ps_p_vy[j] = dy * spd;")
    p("        _ps_p_age[j] = 0.f;")
    p("        _ps_p_life[j] = _ps_life0[s] + (_ps_life1[s] - _ps_life0[s]) * _ps_rand();")
    p("        _ps_p_size[j] = _ps_size0[s] + (_ps_size1[s] - _ps_size0[s]) * _ps_rand();")
    p("        t = _ps_rand();")
    p("        _ps_p_r[j] = _ps_c0r[s] + (_ps_c1r[s] - _ps_c0r[s]) * t;")
    p("        _ps_p_g[j] = _ps_c0g[s] + (_ps_c1g[s] - _ps_c0g[s]) * t;")
    p("        _ps_p_b[j] = _ps_c0b[s] + (_ps_c1b[s] - _ps_c0b[s]) * t;")
    p("        _ps_p_a[j] = _ps_c0a[s] + (_ps_c1a[s] - _ps_c0a[s]) * t;")
    p("        _ps_alive[s] = _ps_alive[s] + 1;")
    p("    }")
    p("}")
    p("/* After LateUpdate: emit (rateOverTime, bursts) while emitting, age,")
    p("   fall (gravityModifier * Unity's -9.81), move; a looping one repeats. */")
    p("static void _ps_update(float frame_dt) {")
    p("    int s, j, k;")
    p("    for (s = 0; s < %d; s = s + 1) {" % len(ps))
    p("        float dt = frame_dt * _ps_sim[s], t0;")
    p("        if (_ps_paused[s] || dt <= 0.f) continue;")
    p("        if (_ps_emitting[s]) {")
    p("            t0 = _ps_time[s];")
    p("            _ps_time[s] = _ps_time[s] + dt;")
    p("            _ps_owed[s] = _ps_owed[s] + _ps_rate[s] * dt;")
    p("            k = (int)_ps_owed[s];")
    p("            if (k > 0) { _ps_emit(s, k); _ps_owed[s] = _ps_owed[s] - (float)k; }")
    p("            for (j = 0; j < _ps_burst_count[s]; j = j + 1) {")
    p("                float bt = _ps_burst_t[_ps_burst_start[s] + j];")
    p("                if (bt >= t0 && bt < _ps_time[s])")
    p("                    _ps_emit(s, (int)_ps_burst_n[_ps_burst_start[s] + j]);")
    p("            }")
    p("            if (_ps_time[s] >= _ps_duration[s]) {")
    p("                if (_ps_looping[s]) _ps_time[s] = _ps_time[s] - _ps_duration[s];")
    p("                else _ps_emitting[s] = 0;")
    p("            }")
    p("        }")
    p("        for (k = 0; k < _ps_alive[s]; ) {")
    p("            j = _ps_base[s] + k;")
    p("            _ps_p_age[j] = _ps_p_age[j] + dt;")
    p("            if (_ps_p_age[j] >= _ps_p_life[j]) {")
    p("                int last = _ps_base[s] + _ps_alive[s] - 1;")
    p("                _ps_p_x[j] = _ps_p_x[last]; _ps_p_y[j] = _ps_p_y[last];")
    p("                _ps_p_vx[j] = _ps_p_vx[last]; _ps_p_vy[j] = _ps_p_vy[last];")
    p("                _ps_p_age[j] = _ps_p_age[last]; _ps_p_life[j] = _ps_p_life[last];")
    p("                _ps_p_size[j] = _ps_p_size[last];")
    p("                _ps_p_r[j] = _ps_p_r[last]; _ps_p_g[j] = _ps_p_g[last];")
    p("                _ps_p_b[j] = _ps_p_b[last]; _ps_p_a[j] = _ps_p_a[last];")
    p("                _ps_alive[s] = _ps_alive[s] - 1;")
    p("                continue;")
    p("            }")
    p("            _ps_p_vy[j] = _ps_p_vy[j] - 9.81f * _ps_gravity[s] * dt;")
    p("            _ps_p_x[j] = _ps_p_x[j] + _ps_p_vx[j] * dt;")
    p("            _ps_p_y[j] = _ps_p_y[j] + _ps_p_vy[j] * dt;")
    p("            k = k + 1;")
    p("        }")
    p("        if (!_ps_emitting[s] && _ps_alive[s] == 0) _ps_playing[s] = 0;")
    p("    }")
    p("}")
    p("")


def emit_collect(p, plan):
    """The draw list's particles: squares of their color (tex -2, none)."""
    if not plan.get("particles"):
        return
    p("static void _ps_collect(EngineDraw *out, int *n, int max) {")
    p("    int s, k;")
    p("    for (s = 0; s < %d; s = s + 1) {" % len(plan["particles"]))
    p("        float ex = 0.f, ey = 0.f;")
    p("        if (!_ps_world[s]) _ps_emitter_pos(s, &ex, &ey);")
    p("        for (k = 0; k < _ps_alive[s] && *n < max; k = k + 1) {")
    p("            int j = _ps_base[s] + k;")
    p("            EngineDraw *d = &out[*n];")
    p("            d->x = ex + _ps_p_x[j];")
    p("            d->y = ey + _ps_p_y[j];")
    p("            d->half_w = _ps_p_size[j] * 0.5f;")
    p("            d->half_h = _ps_p_size[j] * 0.5f;")
    p("            d->m00 = 1.f; d->m01 = 0.f; d->m10 = 0.f; d->m11 = 1.f;")
    p("            d->r = _ps_p_r[j]; d->g = _ps_p_g[j]; d->b = _ps_p_b[j];")
    p("            d->a = _ps_p_a[j];")
    p("            d->tex = -2; /* no texture: its color */")
    p("            d->sorting_layer = 0;")
    p("            d->sorting_order = 0;")
    p("            *n = *n + 1;")
    p("        }")
    p("    }")
    p("}")
    p("")


def lower_api(text, cl, plan, c_ident):
    """ParticleSystem members on a field, local or GetComponent's."""
    if not plan.get("particles"):
        return text
    idn = c_ident(cl["name"])
    text = cs2cpp.code_sub(
        r"(?<![\w.])(?:this\s*\.\s*)?(?:gameObject\s*\.\s*)?GetComponent\s*<\s*"
        r"(?:UnityEngine\s*\.\s*)?ParticleSystem\s*>\s*\(\s*\)",
        "GameObject_GetComponent_ParticleSystem(_engine_go_of_%s(i))" % idn, text)
    recvs = {}
    for f in cl.get("fields") or []:
        if f.get("ty") == "ParticleSystem":
            recvs[r"(?<![\w.])(?:this\s*\.\s*)?%s" % re.escape(f["name"])] = \
                "(int)%s_get_%s(i)" % (idn, f["name"])
    for n in set(re.findall(r"(?<![\w.<])(?:UnityEngine\s*\.\s*)?ParticleSystem\s+(\w+)"
                            r"(?=\s*[=;,)])", cs2cpp._blank(text))):
        recvs[r"(?<![\w.])%s" % re.escape(n)] = n
    call = "GameObject_GetComponent_ParticleSystem(_engine_go_of_%s(i))" % idn
    recvs[re.escape(call)] = call
    text = cs2cpp.code_sub(r"(?<![\w.<])(?:UnityEngine\s*\.\s*)?ParticleSystem(?=\s+\w+\s*[=;])",
                           "int", text)
    for pat, rx in recvs.items():
        text = cs2cpp.code_sub(pat + r"\s*(==|!=)\s*null\b",
                               lambda m, rx=rx: "(%s %s)" % (
                                   rx, "< 0" if m.group(1) == "==" else ">= 0"), text)
        for meth in ("Play", "Stop", "Pause", "Clear"):
            text = cs2cpp.code_sub(pat + r"\s*\.\s*%s\s*\([^()]*\)" % meth,
                                   lambda m, rx=rx, meth=meth: "ParticleSystem_%s(%s)" % (
                                       meth, rx), text)
        text = cs2cpp.code_sub(pat + r"\s*\.\s*Emit\s*\(([^()]*)\)",
                               lambda m, rx=rx: "ParticleSystem_Emit(%s, (int)(%s))" % (
                                   rx, m.group(1).strip() or "1"), text)
        for prop in ("isPlaying", "isEmitting", "isPaused", "isStopped", "particleCount"):
            text = cs2cpp.code_sub(pat + r"\s*\.\s*%s\b" % prop,
                                   lambda m, rx=rx, prop=prop: "ParticleSystem_get_%s(%s)" % (
                                       prop, rx), text)
    return text
