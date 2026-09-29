"""The Input System's InputAction, as the pack sees it: code-defined.

    InputAction jump = new InputAction("Jump", binding: "<Keyboard>/space");
    move.AddCompositeBinding("2DVector")
        .With("Up", "<Keyboard>/w").With("Down", "<Keyboard>/s")
        .With("Left", "<Keyboard>/a").With("Right", "<Keyboard>/d");
    move.AddBinding("<Gamepad>/leftStick");
    jump.Enable();
    if (jump.WasPressedThisFrame()) ..;  Vector2 m = move.ReadValue<Vector2>();

The bindings are string literals, so they are resolved when the project is
packed: each action (a field or local of the type) becomes a slot of the
engine's binding tables -- its constructor's binding, and every AddBinding /
AddCompositeBinding(..).With(..) the class gives it -- and its members are
the engine's `_ia_*(slot)`, which evaluate the bindings each frame over the
keyboard, the gamepad and the mouse wheel the host reports (the controls
Keyboard.current / Gamepad.current / Input.mouseScrollDelta read):

    Enable() / Disable() / enabled            the action's flag (a disabled
                                              action reads 0)
    ReadValue<float>() / ReadValue<Vector2>() the most actuated binding (Unity's
                                              disambiguation); a 2DVector /
                                              Dpad composite is its digital,
                                              normalized vector, a 1DAxis
                                              positive - negative
    IsPressed() / WasPressedThisFrame() /     actuation past 0.5 (Unity's
    WasReleasedThisFrame() / triggered        default press point), and its
                                              edges this frame

An action whose bindings are serialized (set in the Inspector) has none the
pack can read: it is reported, and never pressed. `performed += ..`
callbacks, interactions and processors are not read.
"""
import re

import tools.cs2cpp as cs2cpp

#: <Gamepad>/ controls -> (kind, index): 3 a button (the host's index, 15 / 16
#: the triggers as buttons), 4 a vector (0 left stick, 1 right stick, 2
#: dpad), 5 an axis (the host's).
_GAMEPAD = {
    "buttonSouth": (3, 0), "buttonEast": (3, 1), "buttonWest": (3, 2),
    "buttonNorth": (3, 3), "leftShoulder": (3, 4), "rightShoulder": (3, 5),
    "select": (3, 6), "start": (3, 7), "leftStickPress": (3, 9),
    "rightStickPress": (3, 10), "dpad/up": (3, 11), "dpad/right": (3, 12),
    "dpad/down": (3, 13), "dpad/left": (3, 14),
    "leftTrigger": (5, 4), "rightTrigger": (5, 5),
    "leftStick": (4, 0), "rightStick": (4, 1), "dpad": (4, 2),
    "leftStick/x": (5, 0), "leftStick/y": (5, 1),
    "rightStick/x": (5, 2), "rightStick/y": (5, 3),
}
_PARTS = {"up": 1, "down": 2, "left": 3, "right": 4, "negative": 5,
          "positive": 6}


def control(path):
    """A binding path -> (kind, index, key name) or None. Kinds: 1 a key
    (its name), 2 the mouse (0 scroll), 3-5 the gamepad (see _GAMEPAD)."""
    m = re.match(r"\s*<(\w+)>/(.+?)\s*$", path or "")
    if not m:
        return None
    dev, ctl = m.group(1), m.group(2)
    if dev == "Keyboard":
        key = {"return": "enter"}.get(ctl, ctl)
        return (1, 0, key) if re.match(r"^\w+$", key) else None
    if dev == "Mouse" and ctl == "scroll":
        return (2, 0, None)
    if dev in ("Gamepad", "XInputController", "DualShockGamepad"):
        hit = _GAMEPAD.get(ctl)
        return (hit[0], hit[1], None) if hit else None
    return None


def _str_args(args):
    """(positional string literals, {named: value text}) of an argument list."""
    pos, named = [], {}
    for a in cs2cpp.split_call_args(args):
        a = a.strip()
        nm = re.match(r"^(\w+)\s*:\s*(.+)$", a, re.S)
        if nm:
            named[nm.group(1)] = nm.group(2).strip()
        else:
            pos.append(a)
    return pos, named


def _lit(v):
    m = re.match(r'^"((?:[^"\\]|\\.)*)"$', (v or "").strip())
    return m.group(1) if m else None


def serialized_actions(root):
    """InputAction fields set in the Inspector, from the project's scenes and
    prefabs: {script path: {field: {"type": 0 button / 1 value, "bindings":
    [(path, part, composite)]}}} -- the first instance found of each. A
    composite (m_Flags 4) opens a group its parts (8) join, by m_Name."""
    import os
    guid_cs = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in ("Library", "Temp", "Logs", "obj", ".git")]
        for fn in fns:
            if fn.endswith(".cs.meta"):
                try:
                    m = re.search(r"(?m)^guid:\s*([0-9a-fA-F]+)\s*$",
                                  open(os.path.join(dp, fn), errors="replace").read())
                except OSError:
                    continue
                if m:
                    guid_cs[m.group(1).lower()] = os.path.join(dp, fn[:-5])
    out = {}
    for dp, dns, fns in os.walk(os.path.join(root, "Assets")):
        for fn in fns:
            if not fn.endswith((".unity", ".prefab")):
                continue
            try:
                text = open(os.path.join(dp, fn), errors="replace").read()
            except OSError:
                continue
            if "m_SingletonActionBindings" not in text:
                continue
            for blk in re.split(r"(?m)^--- ", text):
                g = re.search(r"m_Script:\s*\{[^}]*guid:\s*([0-9a-fA-F]+)", blk)
                if not g or "m_SingletonActionBindings" not in blk:
                    continue
                cs = guid_cs.get(g.group(1).lower())
                if not cs:
                    continue
                fields = out.setdefault(os.path.abspath(cs), {})
                for fm in re.finditer(r"(?m)^  (\w+):[ \t]*\n((?:    .*\n?)+)", blk):
                    body = fm.group(2)
                    if "m_SingletonActionBindings" not in body or fm.group(1) in fields:
                        continue
                    ty = re.search(r"(?m)^    m_Type:\s*(\d+)", body)
                    act = {"type": 0 if (ty and ty.group(1) == "1") else 1,
                           "bindings": []}
                    comp = 0
                    for em in re.finditer(r"(?ms)^    - m_Name:(.*?)(?=^    - m_Name:|\Z)",
                                          body):
                        e = "m_Name:" + em.group(1)

                        def f(k):
                            m = re.search(r"(?m)%s:[ \t]*(.*)$" % k, e)
                            return m.group(1).strip() if m else ""
                        flags = int(f("m_Flags") or 0)
                        if flags & 4:
                            comp += 1
                            kind = 2 if f("m_Path").lower() in ("1daxis", "axis") else 1
                            cur = comp * 10 + kind
                        elif flags & 8 and comp:
                            part = _PARTS.get(f("m_Name").lower())
                            if part:
                                act["bindings"].append((f("m_Path"), part, cur))
                        elif f("m_Path"):
                            act["bindings"].append((f("m_Path"), 0, 0))
                    fields[fm.group(1)] = act
    return out


def desugar_input_actions(text, actions, where="", serialized=None):
    """The rewrite, for one file; `actions` (a list) gets its slots, and
    `serialized` ({field: action}, this script's) the Inspector's bindings
    of an action the code gives none."""
    if not re.search(r"(?<![\w.])InputAction\b", cs2cpp._blank(text)):
        return text
    scan = cs2cpp._blank(text)
    names = []
    for m in re.finditer(r"(?<![\w.<])(?:UnityEngine\s*\.\s*InputSystem\s*\.\s*)?"
                         r"(?:InputAction|var)\s+(\w+)\s*(?=[;=])", scan):
        if m.group(0).lstrip().startswith("var"):
            if not re.match(r"\s*=\s*new\s+InputAction\s*\(", scan[m.end():]):
                continue
        if m.group(1) not in names:
            names.append(m.group(1))
    if not names:
        return text
    slots = {}
    for n in names:
        slots[n] = len(actions)
        actions.append({"name": n, "where": where, "type": 0, "bindings": [],
                        "composites": 0})
    for n in names:
        act = actions[slots[n]]
        q = re.escape(n)
        # constructors: `n = new InputAction(..)`, the declaration's too
        for m in re.finditer(r"(?<![\w.])%s\s*=\s*new\s+InputAction\s*\(" % q, scan):
            op = m.end() - 1
            cp = _match(scan, op)
            if cp is None:
                continue
            pos, named = _str_args(text[op + 1:cp])
            b = _lit(named.get("binding"))
            if b is None and len(pos) >= 3:
                b = _lit(pos[2])
            if b:
                act["bindings"].append((b, 0, 0))
            ty = named.get("type") or (pos[1] if len(pos) >= 2 else "")
            if "Value" in ty or "PassThrough" in ty:
                act["type"] = 1
        for m in re.finditer(r"(?<![\w.])%s\s*\.\s*AddBinding\s*\(" % q, scan):
            op = m.end() - 1
            cp = _match(scan, op)
            if cp is not None:
                pos, named = _str_args(text[op + 1:cp])
                b = _lit(named.get("path")) or (_lit(pos[0]) if pos else None)
                if b:
                    act["bindings"].append((b, 0, 0))
        for m in re.finditer(r"(?<![\w.])%s\s*\.\s*AddCompositeBinding\s*\(" % q, scan):
            op = m.end() - 1
            cp = _match(scan, op)
            if cp is None:
                continue
            pos, _named = _str_args(text[op + 1:cp])
            kind = (_lit(pos[0]) if pos else "") or ""
            comp_kind = 2 if kind.lower() in ("1daxis", "axis") else 1
            act["composites"] += 1
            cid = act["composites"] * 10 + comp_kind   # id and 2D / 1D
            end = _statement_end(scan, cp)
            for w in re.finditer(r"\.\s*With\s*\(", scan[cp:end]):
                wop = cp + w.end() - 1
                wcp = _match(scan, wop)
                if wcp is None:
                    continue
                wpos, _ = _str_args(text[wop + 1:wcp])
                if len(wpos) >= 2 and _lit(wpos[0]) and _lit(wpos[1]):
                    part = _PARTS.get(_lit(wpos[0]).lower())
                    if part:
                        act["bindings"].append((_lit(wpos[1]), part, cid))
            act["type"] = 1
    for n in names:
        act = actions[slots[n]]
        ser = (serialized or {}).get(n)
        if not act["bindings"] and ser:
            act["bindings"] = list(ser["bindings"])
            act["type"] = ser["type"]
    text = _desugar_callbacks(text, names, slots)
    # the rewrite: statements that only build the action go (their lines
    # stay), its declaration too, and its members are the engine's
    for n in names:
        q = re.escape(n)
        slot = slots[n]
        for pat in (r"(?<![\w.])%s\s*=\s*new\s+InputAction\s*\(" % q,
                    r"(?<![\w.])%s\s*\.\s*(?:AddBinding|AddCompositeBinding)\s*\(" % q):
            while True:
                scan = cs2cpp._blank(text)
                m = re.search(pat, scan)
                if not m:
                    break
                s0 = m.start()
                # a declaration's type goes with it
                d = re.search(r"(?:(?:public|private|protected|internal|static|readonly)\s+)*"
                              r"(?:UnityEngine\s*\.\s*InputSystem\s*\.\s*)?"
                              r"(?:InputAction|var)\s+$", scan[:s0])
                if d:
                    s0 = d.start()
                e = _statement_end(scan, m.end())
                text = text[:s0] + _blank_keep_lines(text[s0:e + 1]) + text[e + 1:]
        text = cs2cpp.code_sub(
            r"(?:(?:public|private|protected|internal|static|readonly)\s+)*"
            r"(?:\[\s*SerializeField\s*\]\s*)?(?<![\w.<])(?:UnityEngine\s*\.\s*InputSystem\s*\.\s*)?"
            r"InputAction\s+%s\s*;" % q, "", text)
        members = (
            (r"Enable\s*\(\s*\)", "_ia_enable(%d, 1)"),
            (r"Disable\s*\(\s*\)", "_ia_enable(%d, 0)"),
            (r"Dispose\s*\(\s*\)", "_ia_enable(%d, 0)"),
            (r"enabled\b", "_ia_enabled(%d)"),
            (r"ReadValue\s*<\s*(?:UnityEngine\s*\.\s*)?Vector2\s*>\s*\(\s*\)", "_ia_read_v(%d)"),
            (r"ReadValue\s*<\s*float\s*>\s*\(\s*\)", "_ia_read_f(%d)"),
            (r"ReadValue\s*<\s*bool\s*>\s*\(\s*\)", "_ia_pressed(%d)"),
            (r"IsPressed\s*\(\s*\)", "_ia_pressed(%d)"),
            (r"WasPressedThisFrame\s*\(\s*\)", "_ia_down(%d)"),
            (r"WasPerformedThisFrame\s*\(\s*\)", "_ia_down(%d)"),
            (r"WasReleasedThisFrame\s*\(\s*\)", "_ia_up(%d)"),
            (r"triggered\b", "_ia_down(%d)"),
        )
        for pat, rep in members:
            text = cs2cpp.code_sub(r"(?<![\w.])(?:this\s*\.\s*)?%s\s*\.\s*%s" % (q, pat),
                                   rep % slot, text)
    return text


#: Callback subscriptions: (class, handler method) per handler id, and the
#: handlers each class must keep (reachability roots); filled by the pass.
HANDLERS = []
ROOTS = {}
_PHASES = {"started": 1, "performed": 2, "canceled": 4}


def _enclosing_class(scan, pos):
    best = None
    for m in re.finditer(r"(?<![\w.])class\s+(\w+)[^{;]*\{", scan):
        if m.start() > pos:
            break
        close = _match_brace(scan, m.end() - 1)
        if close is not None and close > pos:
            best = (m.group(1), m.end() - 1, close)
    return best


def _match_brace(scan, op):
    depth = 0
    for k in range(op, len(scan)):
        if scan[k] == "{":
            depth += 1
        elif scan[k] == "}":
            depth -= 1
            if depth == 0:
                return k
    return None


def _ctx_uses(body, ctx):
    """An InputAction.CallbackContext's members, as the engine's: the action
    whose callback is running (_ia_cur) and its phase (_ia_phase)."""
    if not ctx:
        return body
    q = re.escape(ctx)
    for pat, rep in (
            (r"ReadValue\s*<\s*(?:UnityEngine\s*\.\s*)?Vector2\s*>\s*\(\s*\)", "_ia_read_v(_ia_cur)"),
            (r"ReadValue\s*<\s*float\s*>\s*\(\s*\)", "_ia_read_f(_ia_cur)"),
            (r"ReadValueAsButton\s*\(\s*\)", "_ia_pressed(_ia_cur)"),
            (r"performed\b", "(_ia_phase == 2)"),
            (r"started\b", "(_ia_phase == 1)"),
            (r"canceled\b", "(_ia_phase == 4)")):
        body = cs2cpp.code_sub(r"(?<![\w.])%s\s*\.\s*%s" % (q, pat), rep, body)
    return body


def _desugar_callbacks(text, names, slots):
    """`action.started / performed / canceled += handler` (a method taking an
    InputAction.CallbackContext, or a lambda) and `-=`: subscriptions of
    (handler, this instance) the engine fires as the action changes."""
    alt = "|".join(re.escape(n) for n in names)
    pat = (r"(?<![\w.])(?:this\s*\.\s*)?(%s)\s*\.\s*(started|performed|canceled)\s*"
           r"([+-])=\s*" % alt)
    extra = []          # (class close offset, method text) to add
    for _pass in range(64):
        scan = cs2cpp._blank(text)
        m = re.search(pat, scan)
        if not m:
            break
        name, phase, sign = m.group(1), m.group(2), m.group(3)
        end = _statement_end(scan, m.end())
        handler = text[m.end():end].strip()
        cls = _enclosing_class(scan, m.start())
        if not cls:
            break
        cname = cls[0]
        lm = re.match(r"^\(?\s*(?:InputAction\s*\.\s*CallbackContext\s+)?(\w+)?\s*\)?\s*=>\s*(.*)$",
                      handler, re.S)
        if lm:
            ctx = lm.group(1)
            body = lm.group(2).strip()
            if not body.startswith("{"):
                body = "{ %s; }" % body
            method = "__ia_cb_%d" % len(HANDLERS)
            extra.append((cname, "    void %s() %s\n" % (method, _ctx_uses(body, ctx))))
        elif re.match(r"^\w+$", handler):
            method = handler
        else:
            break
        key = (cname, method)
        if key not in HANDLERS:
            HANDLERS.append(key)
        ROOTS.setdefault(cname, set()).add(method)
        h = HANDLERS.index(key)
        call = "%s(%d, %d, %d, __ia_self)" % (
            "_ia_subscribe" if sign == "+" else "_ia_unsubscribe",
            slots[name], _PHASES[phase], h)
        text = text[:m.start()] + call + text[end:]
    # method-group handlers: the context parameter goes, its uses are the
    # engine's
    for cname, method in HANDLERS:
        text = re.sub(
            r"(\bvoid\s+%s\s*\()\s*(?:UnityEngine\s*\.\s*InputSystem\s*\.\s*)?"
            r"InputAction\s*\.\s*CallbackContext\s+(\w+)\s*(\))" % re.escape(method),
            lambda m: m.group(1) + m.group(3) + "/*ctx:%s*/" % m.group(2), text)
        cm = re.search(r"\bvoid\s+%s\s*\(\s*\)/\*ctx:(\w+)\*/" % re.escape(method), text)
        if cm:
            scan = cs2cpp._blank(text)
            ob = scan.find("{", cm.end())
            cb = _match_brace(scan, ob) if ob >= 0 else None
            if cb is not None:
                text = (text[:cm.end()] + text[cm.end():ob]
                        + _ctx_uses(text[ob:cb + 1], cm.group(1)) + text[cb + 1:])
            text = text.replace("/*ctx:%s*/" % cm.group(1), "", 1)
    # the lambdas' methods, before their class's closing brace (found again:
    # the edits above moved it)
    for cname, method in extra:
        scan = cs2cpp._blank(text)
        cm = re.search(r"(?<![\w.])class\s+%s\b[^{;]*\{" % re.escape(cname), scan)
        close = _match_brace(scan, cm.end() - 1) if cm else None
        if close is not None:
            text = text[:close] + method + text[close:]
    return text


def _blank_keep_lines(s):
    return "\n" * s.count("\n")


def _match(scan, op):
    depth = 0
    for k in range(op, len(scan)):
        if scan[k] == "(":
            depth += 1
        elif scan[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return None


def _statement_end(scan, k):
    depth = 0
    while k < len(scan):
        c = scan[k]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ";" and depth <= 0:
            return k
        k += 1
    return len(scan) - 1


def _ident(name):
    return cs2cpp.c_ident(name)


def engine_tables(actions):
    """The C the engine gets: binding tables and evaluation, and what the
    bindings need of the pack: (code, keyboard keys, api hints)."""
    keys = []
    binds = []           # (action, kind, index, key, part, composite)
    for a, act in enumerate(actions):
        for path, part, comp in act["bindings"]:
            c = control(path)
            if not c:
                continue
            kind, idx, key = c
            if kind == 1:
                if key not in keys:
                    keys.append(key)
                idx = keys.index(key)
            binds.append((a, kind, idx, part, comp))
    hints = {"Vector2"}
    if keys:
        hints.add("Keyboard.current")
    if any(b[1] >= 3 for b in binds):
        hints.add("Gamepad.current")
    if any(b[1] == 2 for b in binds):
        hints.add("Input.mouseScrollDelta")
    na = max(1, len(actions))
    nb = max(1, len(binds))
    out = []
    p = out.append
    p("/* The Input System's InputActions (code-defined; tools/unity_pack_input.py) */")
    starts, counts = [], []
    for a in range(len(actions)):
        rows = [b for b in binds if b[0] == a]
        starts.append(sum(1 for b in binds if b[0] < a))
        counts.append(len(rows))
    for name, vals in (("start", starts), ("count", counts)):
        p("static const int _ia_%s[%d] = { %s };" % (name, na, ", ".join(
            str(v) for v in vals) or "0"))
    for col, k in (("kind", 1), ("index", 2), ("part", 3), ("comp", 4)):
        p("static const int _ia_b_%s[%d] = { %s };" % (col, nb, ", ".join(
            str(b[k]) for b in binds) or "0"))
    p("static unsigned char _ia_on[%d], _ia_now[%d], _ia_was[%d];" % (na, na, na))
    p("static int _ia_key(int k) {")
    p("    switch (k) {")
    for i, key in enumerate(keys):
        p("    case %d: return engine_keyboard_connected && engine_keyboard_%s;" % (i, key))
    p("    default: return 0;")
    p("    }")
    p("}")
    gp = any(b[1] >= 3 for b in binds)
    sc = any(b[1] == 2 for b in binds)
    p("/* one control: a vector (a stick, the dpad, the wheel) or a value */")
    p("static Vector2 _ia_ctl(int kind, int idx) {")
    p("    switch (kind) {")
    p("    case 1: return Vector2_make((float)_ia_key(idx), 0.f);")
    if sc:
        p("    case 2: return Vector2_make(_engine_scroll_fx, _engine_scroll_fy);")
    if gp:
        p("    case 3: return Vector2_make((float)_gp_btn(idx), 0.f);")
        p("    case 4:")
        p("        if (idx == 0) return Vector2_make(_gp_axis(0), _gp_axis(1));")
        p("        if (idx == 1) return Vector2_make(_gp_axis(2), _gp_axis(3));")
        p("        return Vector2_make((float)(_gp_btn(12) - _gp_btn(14)),")
        p("                            (float)(_gp_btn(11) - _gp_btn(13)));")
        p("    case 5: return Vector2_make(_gp_axis(idx), 0.f);")
    p("    default: return Vector2_make(0.f, 0.f);")
    p("    }")
    p("}")
    p("/* An action's value: its most actuated binding (a composite counted as")
    p("   one: 2D the digital vector, normalized; 1D positive - negative). */")
    p("static Vector2 _ia_read_v(int a) {")
    p("    Vector2 best = Vector2_make(0.f, 0.f), v;")
    p("    float bm = -1.f, m;")
    p("    int b, b1, c;")
    p("    if (a < 0 || a >= %d || !_ia_on[a]) return best;" % len(actions))
    p("    b1 = _ia_start[a] + _ia_count[a];")
    p("    for (b = _ia_start[a]; b < b1; b = b + 1) {")
    p("        c = _ia_b_comp[b];")
    p("        if (c && b > _ia_start[a] && _ia_b_comp[b - 1] == c) continue;")
    p("        if (c) {")
    p("            int k;")
    p("            float x = 0.f, y = 0.f;")
    p("            for (k = b; k < b1 && _ia_b_comp[k] == c; k = k + 1) {")
    p("                float on = _ia_ctl(_ia_b_kind[k], _ia_b_index[k]).x;")
    p("                on = on > 0.5f ? 1.f : 0.f;")
    p("                switch (_ia_b_part[k]) {")
    p("                case 1: y = y + on; break;")
    p("                case 2: y = y - on; break;")
    p("                case 3: case 5: x = x - on; break;")
    p("                case 4: case 6: x = x + on; break;")
    p("                default: break;")
    p("                }")
    p("            }")
    p("            m = x * x + y * y;")
    p("            if (c % 10 == 1 && m > 1.f) {")
    p("                float r = sqrtf(m);")
    p("                x = x / r; y = y / r; m = 1.f;")
    p("            }")
    p("            v = Vector2_make(x, y);")
    p("        } else {")
    p("            v = _ia_ctl(_ia_b_kind[b], _ia_b_index[b]);")
    p("            m = v.x * v.x + v.y * v.y;")
    p("        }")
    p("        if (m > bm) { bm = m; best = v; }")
    p("    }")
    p("    return best;")
    p("}")
    p("static float _ia_read_f(int a) { return _ia_read_v(a).x; }")
    p("static int _ia_actuated(int a) {")
    p("    Vector2 v = _ia_read_v(a);")
    p("    return v.x * v.x + v.y * v.y >= 0.25f; /* the default press point, 0.5 */")
    p("}")
    p("static int _ia_pressed(int a) { return a >= 0 && a < %d && _ia_now[a]; }"
      % len(actions))
    p("static int _ia_down(int a) { return a >= 0 && a < %d && _ia_now[a] && !_ia_was[a]; }"
      % len(actions))
    p("static int _ia_up(int a) { return a >= 0 && a < %d && !_ia_now[a] && _ia_was[a]; }"
      % len(actions))
    p("static void _ia_enable(int a, int on) {")
    p("    if (a >= 0 && a < %d) _ia_on[a] = (unsigned char)(on ? 1 : 0);" % len(actions))
    p("}")
    p("static int _ia_enabled(int a) { return a >= 0 && a < %d && _ia_on[a]; }"
      % len(actions))
    # callbacks: subscriptions (handler, instance, action, phases)
    nsub = max(1, 4 * max(1, len(HANDLERS)) * 8)
    p("/* InputAction callbacks: the running one's action and phase (1 started,")
    p("   2 performed, 4 canceled), and the subscriptions */")
    p("static int _ia_cur = -1, _ia_phase = 0;")
    p("static int _ia_sub_a[%d], _ia_sub_ph[%d], _ia_sub_h[%d], _ia_sub_i[%d];"
      % (nsub, nsub, nsub, nsub))
    p("static int _ia_sub_n = 0;")
    p("static Vector2 _ia_last[%d];" % na)
    for cname, method in HANDLERS:
        p("static void %s_%s(unsigned i);" % (_ident(cname), method))
    p("static void _ia_call(int h, int inst) {")
    p("    switch (h) {")
    for h, (cname, method) in enumerate(HANDLERS):
        p("    case %d: %s_%s((unsigned)inst); break;" % (h, _ident(cname), method))
    p("    default: break;")
    p("    }")
    p("}")
    p("static void _ia_subscribe(int a, int ph, int h, int inst) {")
    p("    int k;")
    p("    for (k = 0; k < _ia_sub_n; k = k + 1)")
    p("        if (_ia_sub_a[k] == a && _ia_sub_ph[k] == ph && _ia_sub_h[k] == h"
      " && _ia_sub_i[k] == inst) return;")
    p("    if (_ia_sub_n >= %d) return;" % nsub)
    p("    _ia_sub_a[_ia_sub_n] = a; _ia_sub_ph[_ia_sub_n] = ph;")
    p("    _ia_sub_h[_ia_sub_n] = h; _ia_sub_i[_ia_sub_n] = inst;")
    p("    _ia_sub_n = _ia_sub_n + 1;")
    p("}")
    p("static void _ia_unsubscribe(int a, int ph, int h, int inst) {")
    p("    int k, j;")
    p("    for (k = 0; k < _ia_sub_n; k = k + 1)")
    p("        if (_ia_sub_a[k] == a && _ia_sub_ph[k] == ph && _ia_sub_h[k] == h"
      " && _ia_sub_i[k] == inst) {")
    p("            for (j = k + 1; j < _ia_sub_n; j = j + 1) {")
    p("                _ia_sub_a[j - 1] = _ia_sub_a[j]; _ia_sub_ph[j - 1] = _ia_sub_ph[j];")
    p("                _ia_sub_h[j - 1] = _ia_sub_h[j]; _ia_sub_i[j - 1] = _ia_sub_i[j];")
    p("            }")
    p("            _ia_sub_n = _ia_sub_n - 1;")
    p("            return;")
    p("        }")
    p("}")
    p("static void _ia_fire(int a, int ph) {")
    p("    int k;")
    p("    for (k = 0; k < _ia_sub_n; k = k + 1)")
    p("        if (_ia_sub_a[k] == a && _ia_sub_ph[k] == ph) {")
    p("            _ia_cur = a;")
    p("            _ia_phase = ph;")
    p("            _ia_call(_ia_sub_h[k], _ia_sub_i[k]);")
    p("        }")
    p("    _ia_cur = -1;")
    p("    _ia_phase = 0;")
    p("}")
    types = [int(act.get("type") or 0) for act in actions] or [0]
    p("static const int _ia_type[%d] = { %s };" % (na, ", ".join(str(t) for t in types)))
    p("/* Each frame, before Update (the Input System's update): the actions'")
    p("   state and their callbacks -- a button: started and performed as it is")
    p("   pressed, canceled as it is released; a value: started as it becomes")
    p("   actuated, performed each time it changes, canceled back at rest. */")
    p("static void _ia_latch(void) {")
    p("    int a;")
    p("    for (a = 0; a < %d; a = a + 1) {" % len(actions))
    p("        Vector2 v;")
    p("        _ia_was[a] = _ia_now[a];")
    p("        _ia_now[a] = (unsigned char)_ia_actuated(a);")
    p("        if (!_ia_sub_n || !_ia_on[a]) continue;")
    p("        if (_ia_now[a] && !_ia_was[a]) { _ia_fire(a, 1); if (!_ia_type[a]) _ia_fire(a, 2); }")
    p("        v = _ia_read_v(a);")
    p("        if (_ia_type[a] && _ia_now[a] && (v.x != _ia_last[a].x || v.y != _ia_last[a].y))")
    p("            _ia_fire(a, 2);")
    p("        if (!_ia_now[a] && _ia_was[a]) _ia_fire(a, 4);")
    p("        _ia_last[a] = v;")
    p("    }")
    p("}")
    return "\n".join(out) + "\n", keys, hints
