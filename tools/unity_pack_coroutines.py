"""Unity coroutines, as a state machine the packer lowers like any method.

A coroutine is an `IEnumerator` method of a MonoBehaviour that `yield`s;
`StartCoroutine` runs it to its first `yield`, and Unity resumes it once a
frame after `Update` until it ends. The packed engine has no iterators, so
each one is rewritten at the source level (in the same overlay as the
extension methods and collections) into things the packer already has --
private fields and methods of its class:

    IEnumerator Blink(int n) {                 private int _co_Blink_state;
        for (int k = 0; k < n; k++) {          private float _co_Blink_t0;
            Debug.Log(k);                      private float _co_Blink_wait;
            yield return                       private int _co_Blink_n;
                new WaitForSeconds(0.5f);      private int _co_Blink_k;
        }
    }                                          private bool _co_Blink_step() {
                                                 switch (_co_Blink_state) {
                                                   case 1: break;
                                                   case 2: goto _co_Blink_L2;
                                                   default: return false; }
                                                 for (_co_Blink_k = 0; ..) {
                                                   Debug.Log(_co_Blink_k);
                                                   { _co_Blink_state = 2;
                                                     _co_Blink_t0 = Time.time;
                                                     _co_Blink_wait = 0.5f;
                                                     return true;
                                                     _co_Blink_L2: ; }
                                                 }
                                                 _co_Blink_state = 0;
                                                 return false; }

* its parameters and locals are fields, so they survive a `yield`; a
  `for` header's declaration is an assignment;
* `yield return null` (or `0`, `WaitForEndOfFrame`, `WaitForFixedUpdate`)
  waits a frame -- an `int` frame count per object, advanced after the
  resumes -- and `yield return new WaitForSeconds(t)` until `Time.time`
  has moved on by `t` (a deadline in integer milliseconds: a class that
  does not move packs its floats as halves); `yield break` ends it;
* `StartCoroutine(Blink(3))` / `("Blink")` / `(nameof(Blink))` is
  `_co_Blink_start(3)`: the parameters set, the body run to its first
  `yield`; `StopCoroutine(..)` / `StopAllCoroutines()` clear the state;
* `_co_tick()` resumes each one whose wait is over, once a frame after the
  class's `Update` -- which is renamed and called from an `Update` made to
  call both, so its early `return` does not skip the coroutines.

Each object has its own fields, so each runs its own; starting one that
is already running restarts it (Unity would run a second). One this does
not read -- `yield return StartCoroutine(..)`, `WaitUntil` (a lambda), a
`yield` in a `foreach`, a `var` whose type it cannot see, a `Coroutine`
kept in a variable -- is left as written, for the stub check. The class's
lines stay on their lines; what is generated goes after its last one.
"""
import re

import tools.cs2cpp as cs2cpp

from tools.unity_pack_collections import _match, _class_body_open

_TYPE = r"[A-Za-z_][\w.]*(?:\s*<[^<>;()]*>)?(?:\s*\[\s*\])?"
_NOT_TYPES = frozenset(("return", "yield", "new", "else", "case", "goto",
                        "throw", "await", "using", "in", "is", "as", "out",
                        "ref"))


def _locals(body):
    """[(type, name)] declared in a coroutine body, in order, or None when
    one cannot be hoisted (a `var` of no visible type)."""
    scan = cs2cpp._blank(body)
    out, seen = [], set()
    for m in re.finditer(r"(?:(?<=[;{}(])|^)\s*(%s)\s+([A-Za-z_]\w*)\s*(?==[^=]|;)"
                         % _TYPE, scan, re.M):
        ty, name = m.group(1).strip(), m.group(2)
        if ty in _NOT_TYPES or ty.split(".")[-1] in _NOT_TYPES:
            continue
        if ty == "var":
            nm = re.match(r"\s*=\s*new\s+(%s)\s*\(" % _TYPE, scan[m.end():])
            if not nm:
                return None
            ty = nm.group(1).strip()
        if name not in seen:
            seen.add(name)
            out.append((ty, name))
    return out


def _transform(name, params, body):
    """(step method body, fields, param list) for one coroutine, or None."""
    scan = cs2cpp._blank(body)
    if re.search(r"\byield\s+return\s+(?!null\b|0\b|new\s+Wait(?:ForSeconds|"
                 r"ForEndOfFrame|ForFixedUpdate)\b)", scan):
        return None                        # StartCoroutine, WaitUntil, ..
    # a yield inside a foreach cannot be resumed
    for m in re.finditer(r"\bforeach\s*\(", scan):
        cp = _match(scan, m.end() - 1, "(", ")")
        k = cp + 1
        while k < len(scan) and scan[k] in " \t\r\n":
            k += 1
        end = _match(scan, k, "{", "}") if k < len(scan) and scan[k] == "{" \
            else scan.find(";", k)
        if end and "yield" in scan[k:end]:
            return None
    locs = _locals(body)
    if locs is None:
        return None
    pre = "_co_%s_" % name
    # Integers, not floats: a class that does not move packs its floats
    # as halves, and a stored time read back below Time.time resumed a
    # `yield return null` in the frame it yielded in.
    fields = [("int", pre + "state"), ("int", pre + "f0"),
              ("int", pre + "until")]
    renames = {}
    for ty, pn in params:
        fields.append((ty, pre + pn))
        renames[pn] = pre + pn
    for ty, ln in locs:
        if ln not in renames:
            fields.append((ty, pre + ln))
            renames[ln] = pre + ln
    # declarations become assignments (a bare `T x;` goes)
    text = body
    for ty, ln in locs:
        text = cs2cpp.code_sub(
            r"(?:(?<=[;{}(])|^)(\s*)%s\s+%s\s*;" % (re.escape(ty), re.escape(ln)),
            lambda m: m.group(1), text)
        text = cs2cpp.code_sub(
            r"(?:(?<=[;{}(])|^)(\s*)(?:%s|var)\s+%s(\s*=[^=])" % (
                re.escape(ty), re.escape(ln)),
            lambda m, ln=ln: m.group(1) + ln + m.group(2), text)
    # uses, by their fields
    for old in sorted(renames, key=len, reverse=True):
        text = cs2cpp.code_sub(r"(?<![\w.])%s(?![\w])" % re.escape(old),
                               renames[old], text)
    # yields
    n = [1]

    def y(m):
        what = m.group(1).strip()
        if what == "break":
            return "{ %sstate = 0; return false; }" % pre
        n[0] += 1
        k = n[0]
        wm = re.match(r"new\s+WaitForSeconds\s*\((.*)\)\s*$", what, re.S)
        wait = "(float)(%s)" % wm.group(1).strip() if wm else "0f"
        return ("{ %sstate = %d; %sf0 = _co_frame; %suntil = (int)((Time.time"
                " + %s) * 1000f); return true; %sL%d: ; }"
                % (pre, k, pre, pre, wait, pre, k))
    text = cs2cpp.code_sub(r"\byield\s+(break|return\s+[^;]*)\s*;",
                           lambda m: y(re.match(r"(?:return\s+)?(.*)",
                                                m.group(1), re.S)), text)
    cases = " ".join("case %d: goto %sL%d;" % (k, pre, k)
                     for k in range(2, n[0] + 1))
    step = ("{ switch (%sstate) { case 1: break; %s default: return false; }"
            "%s %sstate = 0; return false; }"
            % (pre, cases, text.strip()[1:-1], pre))
    return step, fields


def desugar_coroutines(text):
    """The rewrite, for one file."""
    scan = cs2cpp._blank(text)
    head = re.compile(r"(?m)^([ \t]*)((?:(?:public|private|protected|internal)"
                      r"\s+)*)(?:System\s*\.\s*Collections\s*\.\s*)?IEnumerator"
                      r"\s+(\w+)\s*\(([^)]*)\)\s*\{")
    found = []
    for m in head.finditer(scan):
        ob = m.end() - 1
        cb = _match(scan, ob, "{", "}")
        cls = _class_body_open(scan, m.start())
        if cb is None or cls is None:
            continue
        params = []
        for p in cs2cpp.parse_params(text[m.start(4):m.end(4)]):
            params.append((p.type, p.name))
        t = _transform(m.group(3), params, text[ob:cb + 1])
        if t is None:
            continue
        found.append((m, ob, cb, cls, t, params))
    if not found:
        return text
    by_class = {}
    for m, ob, cb, cls, (step, fields), params in reversed(found):
        name = m.group(3)
        pre = "_co_%s_" % name
        # the method becomes the step, on the same lines
        sig = "%sprivate bool %sstep()" % (m.group(1), pre)
        body = step.replace("\n", "\n")
        orig = text[m.start():cb + 1]
        nl = orig.count("\n") - body.count("\n") - sig.count("\n")
        text = text[:m.start()] + sig + " " + body + "\n" * max(0, nl) \
            + text[cb + 1:]
        cm = re.search(r"(?<![\w.])(?:class|struct)\s+(\w+)[^{;]*$",
                       cs2cpp._blank(text[:cls]))
        by_class.setdefault(cm.group(1) if cm else "", []).append(
            (name, fields, params))
    # call sites, in every method of the file
    names = {n for c in by_class.values() for n, _f, _p in c}
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    text = cs2cpp.code_sub(
        r"(?<![\w.])(?:this\s*\.\s*)?StartCoroutine\s*\(\s*(%s)\s*\(([^()]*)\)\s*\)"
        % alt, lambda m: "_co_%s_start(%s)" % (m.group(1), m.group(2)), text)
    # The string forms read the literal, so they match the text itself (the
    # blanked copy has no name in it); the pattern is specific enough.
    text = re.sub(
        r'(?<![\w.])(?:this\s*\.\s*)?StartCoroutine\s*\(\s*(?:"(%s)"|nameof\s*\(\s*(%s)'
        r'\s*\))\s*\)' % (alt, alt),
        lambda m: "_co_%s_start()" % (m.group(1) or m.group(2)), text)
    text = re.sub(
        r'(?<![\w.])(?:this\s*\.\s*)?StopCoroutine\s*\(\s*(?:"(%s)"|nameof\s*\('
        r'\s*(%s)\s*\)|(%s)\s*\([^()]*\))\s*\)' % (alt, alt, alt),
        lambda m: "_co_%s_state = 0" % (m.group(1) or m.group(2) or m.group(3)),
        text)
    # per class: fields, start / tick methods, the Update wrapper
    scan = cs2cpp._blank(text)
    for cls in sorted(by_class):
        scan = cs2cpp._blank(text)
        hm = re.search(r"(?<![\w.])(?:class|struct)\s+%s\b[^{;]*\{"
                       % re.escape(cls), scan)
        if not hm:
            continue
        ob = hm.end() - 1
        cb = _match(scan, ob, "{", "}")
        cos = by_class[cls]
        stop_all = " ".join("_co_%s_state = 0;" % n for n, _f, _p in cos)
        text_cls = text[ob:cb]
        text_cls = cs2cpp.code_sub(
            r"(?<![\w.])(?:this\s*\.\s*)?StopAllCoroutines\s*\(\s*\)",
            "{ %s }" % stop_all, text_cls)
        text = text[:ob] + text_cls + text[cb:]
        scan = cs2cpp._blank(text)
        cb = _match(scan, ob, "{", "}")
        tail = []
        for name, fields, params in cos:
            pre = "_co_%s_" % name
            for ty, fn in fields:
                tail.append("private %s %s;" % (ty, fn))
            plist = ", ".join("%s %s" % (t, p) for t, p in params)
            sets = " ".join("%s%s = %s;" % (pre, p, p) for _t, p in params)
            tail.append("private void %sstart(%s) { %s %sstate = 1; "
                        "%sstep(); }" % (pre, plist, sets, pre, pre))
        # A frame count per object, advanced after the resumes: a yield in
        # Start, or in a resume, waits for the next frame's.
        tick = " ".join(
            "if (_co_%s_state != 0 && _co_frame > _co_%s_f0 && "
            "(int)(Time.time * 1000f) >= _co_%s_until) _co_%s_step();"
            % ((n,) * 4) for n, _f, _p in cos)
        tail.append("private int _co_frame;")
        tail.append("private void _co_tick() { %s _co_frame = _co_frame + 1; }"
                    % tick)
        # Update: the author's, renamed, then the coroutines
        body = scan[ob:cb]
        um = re.search(r"(?m)^([ \t]*)((?:(?:public|private|protected)\s+)*)"
                       r"void\s+Update\s*\(\s*\)", body)
        if um:
            at = ob + um.start()
            seg = text[at:ob + um.end()]
            text = text[:at] + seg.replace("Update", "_co_user_Update", 1) \
                + text[ob + um.end():]
            cb += len("_co_user_Update") - len("Update")
            tail.append("void Update() { _co_user_Update(); _co_tick(); }")
        else:
            tail.append("void Update() { _co_tick(); }")
        text = text[:cb].rstrip(" \t") + "\n" + "\n".join(
            " " + t for t in tail) + "\n" + text[cb:]
    return text
