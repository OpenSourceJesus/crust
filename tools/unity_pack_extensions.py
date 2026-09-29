"""Static helper classes and extension methods, as C# the packer already reads.

A `static class` holds no instances, so the packer has nothing to pack it
as. What it can lower is a MonoBehaviour's own static method -- with every
rewrite a method gets: strings, the runtime table, packed fields. So this
works on the *source*, before the analysis sees it:

* a C# 14 extension block --

      extension (GameObject go) {
          public bool IsActive => go.activeInHierarchy;
          public T GetOrAdd<T>() where T : Component { .. }
      }

  -- becomes the classic static form, the receiver the first parameter:
  `public static bool get_IsActive(this GameObject go) => ..;` and
  `public static T GetOrAdd<T>(this GameObject go) where T : Component`;
* an extension call, `x.M(a)` / `x.M<T>(a)` / `x.P`, becomes the static
  call `Cls.M(x, a)` / `Cls.M<T>(x, a)` / `Cls.get_P(x)`;
* a static method called from a class, whose body is more than one
  `return expr;` (those the packer already inlines), or which is generic,
  is copied into the calling class as a private static method --
  `Cls__M`, or `Cls__M__Rigidbody2D` for each type argument, `T`
  substituted -- and the call renamed. Its calls to its siblings, its
  class's consts and extension calls inside it are rewritten the same way,
  and what it calls is copied too.

Every line of a class stays on its line -- the copies go after its last
one, each on its own -- so a diagnostic still points where the author
wrote; only another top-level type later in the same file moves down. A
method that reads or writes a non-const static field of its class is not
copied -- each class would get its own copy of shared state -- and its
calls are left for the stub check. Type arguments must be written out;
inferred ones (`x.M()` for `M<T>(this T x)`) are not.
"""
import re

import tools.cs2cpp as cs2cpp

#: Extension methods the packer lowers itself, from the call alone (its own
#: `RectTransform_GetWorldRect`, `_engine_set_world_scale`, the static-array
#: `Add` / `Remove`): left exactly as written.
NATIVE_EXTENSIONS = frozenset((
    "GetWorldRect", "SetWorldScale", "Add", "Remove"))

#: Extension methods the packer lowers itself only inside another call's
#: argument (`t.SetWorldScale(v.SetX(a).SetZ(1))`): left as written there,
#: and rewritten like any other everywhere else.
NATIVE_IN = {"SetX": "SetWorldScale", "SetY": "SetWorldScale",
             "SetZ": "SetWorldScale"}


def _inside_call_of(scan, k, name):
    """Is index k inside the argument list of a `.name(` call?"""
    depth = 0
    j = k - 1
    while j >= 0:
        c = scan[j]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                if re.search(r"\.\s*%s\s*$" % re.escape(name), scan[:j]):
                    return True
            else:
                depth -= 1
        elif c in ";{}" and depth == 0:
            return False
        j -= 1
    return False


def _match(scan, k, o, c):
    depth = 0
    while k < len(scan):
        if scan[k] == o:
            depth += 1
        elif scan[k] == c:
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return None


def _strip_comments(text):
    """Comments as spaces (newlines kept), string literals untouched."""
    scan = cs2cpp._blank(text)
    out, in_str = [], None
    for k, ch in enumerate(text):
        s = scan[k]
        if in_str:
            out.append(ch)
            if s == in_str:
                in_str = None
            continue
        if s in "\"'" and ch == s:
            in_str = s
            out.append(ch)
            continue
        out.append(ch if ch == s or ch == "\n" else " ")
    return "".join(out)


# ---------------------------------------------------------------------------
# C# 14 extension blocks -> classic extension methods
# ---------------------------------------------------------------------------

def desugar_extension_blocks(text):
    """Rewrite `extension (T x) { members }` in place, line for line."""
    for _pass in range(64):
        scan = cs2cpp._blank(text)
        m = re.search(r"(?<![\w.])extension\s*(<[^>]*>)?\s*\(\s*([^)]*?)\s*\)"
                      r"\s*\{", scan)
        if not m:
            break
        gen = m.group(1) or ""
        recv = text[m.start(2):m.end(2)].strip()
        ob = m.end() - 1
        cb = _match(scan, ob, "{", "}")
        if cb is None:
            break
        inner = _desugar_members(text[ob + 1:cb], recv, gen)
        # The header and the braces go; their lines stay.
        head = re.sub(r"[^\n]", " ", text[m.start():ob + 1])
        text = text[:m.start()] + head + inner + " " + text[cb + 1:]
    return text


def _desugar_members(body, recv, gen):
    scan = cs2cpp._blank(body)
    out, last = [], 0
    member = re.compile(
        r"(?m)^([ \t]*)((?:(?:public|private|internal|protected)\s+)*)"
        r"(?!static\b)([\w.<>\[\],?]+)\s+(\w+)\s*(<[^>(]*>)?\s*(\(|=>|\{)")
    for m in member.finditer(scan):
        if m.start() < last:
            continue
        indent, mods, ret, name, mgen = (m.group(1), m.group(2), m.group(3),
                                         m.group(4), m.group(5) or "")
        if ret in ("return", "new", "if", "else", "var"):
            continue
        tok = m.group(6)
        gens = _join_generics(gen, mgen)
        if tok == "(":
            op = m.end() - 1
            cp = _match(scan, op, "(", ")")
            if cp is None:
                continue
            params = body[op + 1:cp].strip()
            sig = "%s%sstatic %s %s%s(this %s%s)" % (
                indent, mods, ret, name, gens, recv,
                (", " + params) if params else "")
            out.append(body[last:m.start()])
            out.append(sig)
            last = cp + 1
        else:
            # A property: `=> expr;` or `{ get { .. } }`
            if tok == "{":
                ob = m.end() - 1
                cb = _match(scan, ob, "{", "}")
                if cb is None:
                    continue
                g = re.search(r"\bget\s*(\{|=>)", scan[ob:cb])
                if not g:
                    continue
                gk = ob + g.end() - 1
                if scan[gk] == "{":
                    ge = _match(scan, gk, "{", "}")
                    getter = body[gk:ge + 1]
                else:
                    ge = scan.index(";", gk)
                    getter = body[gk:ge + 1]
                keep = re.sub(r"[^\n]", " ", body[m.start():cb + 1])
                nl = keep.count("\n")
                sig = "%s%sstatic %s get_%s%s(this %s) %s" % (
                    indent, mods, ret, name, gens, recv,
                    getter.replace("\n", " "))
                out.append(body[last:m.start()])
                out.append(sig + "\n" * nl)
                last = cb + 1
            else:
                sig = "%s%sstatic %s get_%s%s(this %s) =>" % (
                    indent, mods, ret, name, gens, recv)
                out.append(body[last:m.start()])
                out.append(sig)
                last = m.end()
    out.append(body[last:])
    return "".join(out)


def _blank_copied_methods(seg):
    """A static class's text with every method that is copied into its
    callers -- all but the one-line, non-generic ones the packer inlines --
    blanked, newlines kept: the analysis reads the class too, and a
    generic `T` there is not a type it knows."""
    scan = cs2cpp._blank(seg)
    head = re.compile(
        r"(?m)^[ \t]*(?:(?:public|private|internal|protected)\s+)*static"
        r"\s+([\w.<>\[\],?]+)\s+(\w+)\s*(<[^>(]*>)?\s*\(")
    spans = []
    for h in head.finditer(scan):
        if h.group(1) in ("class", "struct", "readonly"):
            continue
        cp = _match(scan, h.end() - 1, "(", ")")
        if h.group(2) in NATIVE_EXTENSIONS and re.match(
                r"\s*this\s", seg[h.end():cp]):
            continue                      # the packer lowers the call itself
        if cp is None:
            continue
        k = cp + 1
        wm = re.match(r"\s*where\s+[^{=]*", scan[k:])
        if wm:
            k += wm.end()
        while k < len(scan) and scan[k] in " \t\r\n":
            k += 1
        if scan.startswith("=>", k):
            e = scan.index(";", k)
            single = True
        elif k < len(scan) and scan[k] == "{":
            e = _match(scan, k, "{", "}")
            if e is None:
                continue
            stm = [x for x in scan[k + 1:e].split(";") if x.strip()]
            single = len(stm) == 1 and "{" not in scan[k + 1:e]
        else:
            continue
        if single and not h.group(3):
            continue
        spans.append((h.start(), e + 1))
    for a, b in reversed(spans):
        seg = seg[:a] + re.sub(r"[^\n]", " ", seg[a:b]) + seg[b:]
    return seg


def _join_generics(a, b):
    names = [x.strip() for g in (a, b) if g
             for x in g.strip()[1:-1].split(",") if x.strip()]
    return "<%s>" % ", ".join(names) if names else ""


# ---------------------------------------------------------------------------
# What the static classes hold
# ---------------------------------------------------------------------------

class Method(object):
    def __init__(self, cls, name, gens, params, ret, body, is_ext, single,
                 uses_state):
        self.cls, self.name, self.gens = cls, name, gens
        self.params, self.ret, self.body = params, ret, body
        self.is_ext, self.single, self.uses_state = is_ext, single, uses_state


def static_classes(text):
    """{class: {"methods": {name: [Method]}, "consts": {name: decl},
    "span": (start, end)}} for the top-level static classes in `text`."""
    out = {}
    scan = cs2cpp._blank(text)
    for m in re.finditer(r"(?<![\w.])static\s+(?:partial\s+)?class\s+(\w+)"
                         r"[^{;]*\{", scan):
        name = m.group(1)
        ob = m.end() - 1
        cb = _match(scan, ob, "{", "}")
        if cb is None:
            continue
        body = text[ob + 1:cb]
        bscan = scan[ob + 1:cb]
        consts, state = {}, set()
        for f in re.finditer(r"(?m)^[ \t]*(?:(?:public|private|internal)\s+)?"
                             r"(const|static)\s+(?:readonly\s+)?([\w.<>\[\]]+)"
                             r"\s+(\w+)\s*(=[^;]*)?;", bscan):
            if f.group(2) in ("class", "struct"):
                continue
            decl = body[f.start():f.end()].strip()
            if f.group(1) == "const":
                consts[f.group(3)] = decl
            else:
                state.add(f.group(3))
        methods = {}
        head = re.compile(
            r"(?m)^[ \t]*(?:(?:public|private|internal|protected)\s+)*static"
            r"\s+([\w.<>\[\],?]+)\s+(\w+)\s*(<[^>(]*>)?\s*\(")
        for h in head.finditer(bscan):
            if h.group(1) in ("class", "struct", "readonly"):
                continue
            op = h.end() - 1
            cp = _match(bscan, op, "(", ")")
            if cp is None:
                continue
            params = body[op + 1:cp].strip()
            k = cp + 1
            wm = re.match(r"\s*where\s+[^{=]*", bscan[k:])
            if wm:
                k += wm.end()
            rest = bscan[k:]
            lead = len(rest) - len(rest.lstrip())
            k += lead
            if bscan.startswith("=>", k):
                e = bscan.index(";", k)
                btxt = "{ %s%s; }" % (
                    "" if h.group(1) == "void" else "return ",
                    body[k + 2:e].strip())
                single = True
            elif k < len(bscan) and bscan[k] == "{":
                e = _match(bscan, k, "{", "}")
                btxt = body[k:e + 1]
                stm = [x for x in cs2cpp._blank(btxt)[1:-1].split(";")
                       if x.strip()]
                single = len(stm) == 1 and "{" not in btxt[1:-1]
            else:
                continue
            gens = [g.strip() for g in (h.group(3) or "<>")[1:-1].split(",")
                    if g.strip()]
            uses = bool(state) and bool(re.search(
                r"(?<![\w.])(?:%s)(?![\w])" % "|".join(map(re.escape, state)),
                cs2cpp._blank(btxt)))
            is_ext = params.startswith("this ")
            methods.setdefault(h.group(2), []).append(Method(
                name, h.group(2), gens, params, h.group(1),
                _strip_comments(btxt), is_ext, single, uses))
        out[name] = {"methods": methods, "consts": consts,
                     "span": (m.start(), cb + 1)}
    return out


# ---------------------------------------------------------------------------
# Call sites
# ---------------------------------------------------------------------------

def _receiver_start(scan, dot):
    """Start of the receiver expression that ends just before `dot`."""
    j = dot - 1
    while j >= 0 and scan[j] in " \t":
        j -= 1
    if j < 0:
        return None
    k = j
    while True:
        c = scan[k]
        if c in ")]":
            o = "(" if c == ")" else "["
            depth = 0
            while k >= 0:
                if scan[k] == c:
                    depth += 1
                elif scan[k] == o:
                    depth -= 1
                    if depth == 0:
                        break
                k -= 1
            if k < 0:
                return None
            k -= 1
            while k >= 0 and (scan[k].isalnum() or scan[k] in "_<>"):
                k -= 1
        elif c == '"':
            k -= 1
            while k >= 0 and scan[k] != '"':
                k -= 1
            k -= 1
        elif c.isalnum() or c == "_":
            while k >= 0 and (scan[k].isalnum() or scan[k] == "_"):
                k -= 1
        else:
            return None
        if k >= 0 and scan[k] == ".":
            k -= 1
            continue
        return k + 1


def rewrite_extension_calls(text, exts, props, skip_names=()):
    """`x.M(a)` -> `Cls.M(x, a)`, `x.M<T>(a)` -> `Cls.M<T>(x, a)`, `x.P` ->
    `Cls.get_P(x)`. `exts` / `props`: name -> class. A name that is also a
    method of one of the project's own classes (`skip_names`) is left."""
    names = {n: c for n, c in exts.items() if n not in skip_names}
    if names:
        pat = re.compile(r"\.\s*(%s)\s*(<[^<>()]*>)?\s*\(" % "|".join(
            re.escape(n) for n in sorted(names, key=len, reverse=True)))
        for _pass in range(256):
            scan = cs2cpp._blank(text)
            done = False
            for m in pat.finditer(scan):
                rs = _receiver_start(scan, m.start())
                if rs is None or scan[rs:m.start()].strip() in names.values():
                    continue
                outer = NATIVE_IN.get(m.group(1))
                if outer and _inside_call_of(scan, rs, outer):
                    continue
                recv = text[rs:m.start()].strip()
                op = m.end() - 1
                cp = _match(scan, op, "(", ")")
                if cp is None:
                    continue
                args = text[op + 1:cp].strip()
                cls = names[m.group(1)]
                rep = "%s.%s%s(%s%s)" % (cls, m.group(1), m.group(2) or "",
                                         recv, (", " + args) if args else "")
                text = text[:rs] + rep + text[cp + 1:]
                done = True
                break
            if not done:
                break
    pnames = {n: c for n, c in props.items() if n not in skip_names}
    if pnames:
        pat = re.compile(r"\.\s*(%s)\b(?!\s*[(<=])" % "|".join(
            re.escape(n) for n in sorted(pnames, key=len, reverse=True)))
        for _pass in range(256):
            scan = cs2cpp._blank(text)
            done = False
            for m in pat.finditer(scan):
                rs = _receiver_start(scan, m.start())
                if rs is None:
                    continue
                recv = text[rs:m.start()].strip()
                if recv in pnames.values():
                    continue
                rep = "%s.get_%s(%s)" % (pnames[m.group(1)], m.group(1), recv)
                text = text[:rs] + rep + text[m.end():]
                done = True
                break
            if not done:
                break
    return text


def _mangle(cls, name, targs):
    t = "__".join(re.sub(r"\W", "_", a.strip()) for a in targs)
    return "%s__%s%s" % (cls, name, ("__" + t) if t else "")


def _split_targs(s):
    if not s:
        return []
    return [a.strip() for a in s.strip()[1:-1].split(",") if a.strip()]


def desugar_project(files):
    """{path: text} -> {path: new text} for the files that change."""
    texts = {p: desugar_extension_blocks(t) for p, t in files.items()}
    classes = {}
    for p, t in texts.items():
        for name, info in static_classes(t).items():
            info["path"] = p
            classes[name] = info
    if not classes:
        return {p: t for p, t in texts.items() if t != files[p]}
    exts, props = {}, {}
    for cname, info in classes.items():
        for mname, ms in info["methods"].items():
            for m in ms:
                if m.is_ext and mname in NATIVE_EXTENSIONS:
                    continue
                if m.is_ext:
                    if mname.startswith("get_") and not m.params.count(","):
                        props[mname[4:]] = cname
                    else:
                        exts[mname] = cname
    # Methods of the project's own (non-static) classes shadow extensions.
    own = set()
    for p, t in texts.items():
        for mm in re.finditer(r"(?m)^[ \t]*(?:(?:public|private|protected|"
                              r"internal|override|virtual)\s+)*"
                              r"(?!static\b)[\w.<>\[\]]+\s+(\w+)\s*\(",
                              cs2cpp._blank(t)):
            own.add(mm.group(1))
    static_spans = {(info["path"], info["span"]) for info in classes.values()}

    def copyable(cls, name, targs):
        ms = [m for m in classes[cls]["methods"].get(name, [])
              if len(m.gens) == len(targs)]
        if len(ms) != 1 or ms[0].uses_state:
            return None
        return ms[0]

    def rewrite_static_calls(t, requests):
        """`Cls.M<T>(..)` -> `Cls__M__T(..)` for what gets copied."""
        pat = re.compile(r"(?<![\w.])(%s)\s*\.\s*(\w+)\s*(<[^<>()]*>)?\s*\("
                         % "|".join(re.escape(c) for c in classes))
        start = 0
        for _pass in range(512):
            scan = cs2cpp._blank(t)
            m = pat.search(scan, start)
            if not m:
                break
            cls, name = m.group(1), m.group(2)
            targs = _split_targs(t[m.start(3):m.end(3)] if m.group(3) else "")
            meth = copyable(cls, name, targs)
            if meth is None or (meth.single and not meth.gens) \
                    or (meth.is_ext and name in NATIVE_EXTENSIONS):
                start = m.end()
                continue
            mangled = _mangle(cls, name, targs)
            requests.append((cls, name, tuple(targs)))
            t = t[:m.start()] + mangled + "(" + t[m.end():]
            start = m.start() + len(mangled)
        # consts
        for cls, info in classes.items():
            for k in info["consts"]:
                if re.search(r"(?<![\w.])%s\s*\.\s*%s\b" % (cls, k),
                             cs2cpp._blank(t)):
                    requests.append((cls, "#const", (k,)))
                    t = cs2cpp.code_sub(
                        r"(?<![\w.])%s\s*\.\s*%s\b" % (cls, k),
                        "%s__%s" % (cls, k), t)
        return t

    def copy_text(cls, name, targs):
        if name == "#const":
            decl = classes[cls]["consts"][targs[0]]
            return "private " + re.sub(
                r"\b%s\b" % re.escape(targs[0]), "%s__%s" % (cls, targs[0]),
                re.sub(r"^(?:(?:public|private|internal)\s+)", "", decl),
                count=1)
        m = copyable(cls, name, list(targs))
        body = m.body
        params = re.sub(r"^this\s+", "", m.params)
        ret = m.ret
        for g, a in zip(m.gens, targs):
            sub = r"(?<![\w.])%s(?![\w])" % re.escape(g)
            body = cs2cpp.code_sub(sub, a, body)
            params = cs2cpp.code_sub(sub, a, params)
            ret = cs2cpp.code_sub(sub, a, ret)
        # Its siblings and consts, by their qualified names.
        info = classes[cls]
        for sib in info["methods"]:
            body = cs2cpp.code_sub(r"(?<![\w.])(%s)\s*(?=[<(])" % re.escape(sib),
                                   "%s.%s" % (cls, sib), body)
        for k in info["consts"]:
            body = cs2cpp.code_sub(r"(?<![\w.])%s(?![\w(])" % re.escape(k),
                                   "%s.%s" % (cls, k), body)
        return "private static %s %s(%s) %s" % (
            ret, _mangle(cls, name, targs), params, body)

    out = {}
    for p, t in texts.items():
        scan = cs2cpp._blank(t)
        # The classes of this file that are not static, innermost last.
        spans = []
        for m in re.finditer(r"(?<![\w.])class\s+(\w+)[^{;]*\{", scan):
            if any(sp == p and a <= m.start() < b
                   for sp, (a, b) in static_spans):
                continue
            cb = _match(scan, m.end() - 1, "{", "}")
            if cb is not None:
                spans.append((m.start(), cb))
        if not spans:
            out[p] = t
            continue
        # Rewrite each class's text on its own, and append its copies.
        for a, b in sorted(spans, key=lambda s: -s[0]):
            seg = t[a:b]
            seg = rewrite_extension_calls(seg, exts, props, own)
            reqs = []
            seg = rewrite_static_calls(seg, reqs)
            copies, seen = [], set()
            while reqs:
                r = reqs.pop(0)
                if r in seen:
                    continue
                seen.add(r)
                c = copy_text(*r)
                if r[1] != "#const":
                    c = rewrite_extension_calls(c, exts, props, own)
                    c = rewrite_static_calls(c, reqs)
                copies.append(c.replace("\n", " "))
            if copies:
                # One per line: the analysis finds a method at a line's
                # start. The class's own lines stay put; only what follows
                # its closing brace in the same file moves down.
                seg = seg.rstrip(" \t") + "\n" + "\n".join(
                    " " + c for c in copies) + "\n"
            t = t[:a] + seg + t[b:]
        out[p] = t
    # A static class's own text: its extension methods as plain static
    # ones (the packer's inliner reads the single-return ones there).
    for p in {info["path"] for info in classes.values()}:
        t = out[p]
        for _name, info in sorted(static_classes(t).items(),
                                  key=lambda kv: -kv[1]["span"][0]):
            a, b = info["span"]
            seg = _blank_copied_methods(t[a:b])
            t = t[:a] + re.sub(r"\(\s*this\s+", "(", seg) + t[b:]
        out[p] = t
    return {p: t for p, t in out.items() if t != files[p]}
