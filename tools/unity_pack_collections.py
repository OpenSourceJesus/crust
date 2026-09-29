"""`Stack<T>`, `Queue<T>` and `HashSet<T>`, as the `List<T>` the packer has.

The packer lowers `List<T>` -- a local, an instance field (a table per
instance), a static -- to cpprust's vector, with its members. These three
collections are that list with other members, so they are rewritten at the
source level (in the same overlay as the extension methods), and everything
the list lowering does -- element types, fields, statics, `foreach` --
applies to them unchanged:

    Stack<int> s = new Stack<int>();      List<int> s = new List<int>();
    s.Push(x);                            s.Add(x);
    s.Peek()                              s[s.Count - 1]
    int t = s.Pop();                      _cs_require_nonempty(s.Count, ..);
                                          int _cs_pop1 = s[s.Count - 1];
                                          s.RemoveAt(s.Count - 1);
                                          int t = _cs_pop1;
    foreach (int v in s) ..               top of the stack first, as .NET
    q.Enqueue(x);  q.Dequeue()  q.Peek()  s.Add(x);  the front;  q[0]
    h.Add(x);                             if (!h.Contains(x)) h.Add(x);
    if (h.Add(x)) ..                      the add hoisted, its bool kept
    h.UnionWith(o); IntersectWith; ExceptWith   loops over the lists

`Pop` / `Dequeue` / a valued `HashSet.Add` take and remove in one
expression, so each is hoisted before its statement (in braces unless the
statement declares a local); `Pop` or `Dequeue` on an empty collection
aborts with .NET's InvalidOperationException. One inside a `while` / `for`
header cannot be hoisted -- it runs each time round -- and is left for the
stub check. A `HashSet` keeps insertion order (what .NET's shows until an
element is removed) and its `Contains` is linear; a `Queue`'s `Dequeue`
moves the rest down. Every line stays on its line.
"""
import re

import tools.cs2cpp as cs2cpp

_KINDS = ("Stack", "Queue", "HashSet")
_Q = r"(?:System\s*\.\s*Collections\s*\.\s*Generic\s*\.\s*)?"


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


def _statement_start(scan, k):
    """Start of the statement holding k: after the `;`, `{`, `}` before it
    at its depth, or after a brace-less `if (..)` / `else` / .. head."""
    depth = 0
    j = k - 1
    while j >= 0:
        c = scan[j]
        if c in ")]":
            depth += 1
        elif c in "([":
            if depth == 0:
                # Leaving a bracket we are inside: a call's or a group's is
                # part of the statement; a control header's is not ours.
                kw = re.search(r"(?<![\w])(if|while|for|foreach|switch|using|"
                               r"lock)\s*$", scan[:j]) if c == "(" else None
                if kw:
                    # An `if` / `switch` condition runs once: hoist before
                    # the keyword -- when the statement is not itself a
                    # brace-less body. A loop's runs each time: not ours.
                    if kw.group(1) not in ("if", "switch"):
                        return None
                    before = scan[:kw.start()].rstrip()
                    if before and before[-1] not in ";{}":
                        return None
                    return ("head", kw.start())
                j -= 1
                continue
            depth -= 1
            if depth == 0 and c == "(" and re.search(
                    r"(?<![\w])(?:if|while|for|foreach|using|lock)\s*$",
                    scan[:j]):
                # the closing paren of a control header ends a statement
                # head: the body starts after it
                close = _match(scan, j, "(", ")")
                if close is not None and close < k:
                    j = close
                    break
        elif c in ";{}" and depth == 0:
            break
        elif depth == 0 and re.search(r"(?<![\w])else$", scan[:j + 1]) and \
                c == "e":
            break
        j -= 1
    j += 1
    while j < k and scan[j] in " \t\r\n":
        j += 1
    return j


def _header_of(scan, k):
    """If k lies in a `while (..)` / `for (..)` header, True."""
    depth = 0
    j = k - 1
    while j >= 0:
        c = scan[j]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                return bool(re.search(r"(?<![\w])(?:while|for)\s*$",
                                      scan[:j]))
            depth -= 1
        elif c in ";{}" and depth == 0:
            return False
        j -= 1
    return False


def _statement_end(scan, k):
    depth = 0
    while k < len(scan):
        c = scan[k]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return None
            depth -= 1
        elif c == ";" and depth == 0:
            return k
        k += 1
    return None


def declarations(text):
    """{name: (kind, element type)} for the Stack / Queue / HashSet
    variables and fields `text` declares."""
    scan = cs2cpp._blank(text)
    out = {}
    for m in re.finditer(r"(?<![\w.])%s(%s)\s*<\s*([^<>;]+?)\s*>\s+(\w+)"
                         % (_Q, "|".join(_KINDS)), scan):
        out[m.group(3)] = (m.group(1), text[m.start(2):m.end(2)].strip())
    for m in re.finditer(r"(?<![\w.])var\s+(\w+)\s*=\s*new\s+%s(%s)\s*<\s*"
                         r"([^<>;]+?)\s*>" % (_Q, "|".join(_KINDS)), scan):
        out[m.group(1)] = (m.group(2), text[m.start(3):m.end(3)].strip())
    return out


def _explicit_list_vars(text):
    """`var xs = new List<T>(..)` -> `List<T> xs = new List<T>(..)`: the
    packed list lowering reads the declared type (a `var` one's right-hand
    side was lowered as a temporary, which it refuses)."""
    return cs2cpp.code_sub(
        r"(?<![\w.])var\s+(\w+)(\s*=\s*new\s+%s(?:List|Stack|Queue|HashSet)"
        r"\s*<\s*([^<>;]+?)\s*>)" % _Q,
        lambda m: "%s<%s> %s%s" % (
            re.search(r"(List|Stack|Queue|HashSet)\s*<", m.group(2)).group(1),
            m.group(3), m.group(1), m.group(2)), text)


def desugar_collections(text, counter=None):
    """The rewrite, for one file. `counter` numbers the temporaries."""
    text = _explicit_list_vars(text)
    decls = declarations(text)
    if not decls:
        return cs2cpp.code_sub(r"(?<![\w.])%sList(\s*<)" % _Q,
                               lambda m: "List" + m.group(1), text) \
            if re.search(r"Collections\s*\.\s*Generic\s*\.\s*List", text) \
            else text
    counter = counter if counter is not None else [0]
    alt = "|".join(re.escape(n) for n in sorted(decls, key=len, reverse=True))
    recv = r"(?<![\w.])(?:this\s*\.\s*)?(%s)" % alt

    def fresh(stem):
        counter[0] += 1
        return "_cs_%s%d" % (stem, counter[0])

    # foreach over a Stack: top first.
    for _pass in range(64):
        scan = cs2cpp._blank(text)
        m = re.search(r"(?<![\w.])foreach\s*\(\s*(?:var|[\w.<>\[\]]+)\s+(\w+)"
                      r"\s+in\s+(?:this\s*\.\s*)?(%s)\s*\)" % alt, scan)
        if not m or decls[m.group(2)][0] != "Stack":
            if not m:
                break
            # not a stack: mark past it
            text = text[:m.start()] + "foreach_" + text[m.start() + 7:]
            continue
        var, name = m.group(1), m.group(2)
        elem = decls[name][1]
        k = m.end()
        while k < len(scan) and scan[k] in " \t\r\n":
            k += 1
        if k < len(scan) and scan[k] == "{":
            e = _match(scan, k, "{", "}")
            body = text[k + 1:e]
            end = e + 1
        else:
            e = _statement_end(scan, k)
            if e is None:
                break
            body = text[k:e + 1]
            end = e + 1
        idx = fresh("k")
        loop = ("for (int %s = %s.Count - 1; %s >= 0; %s = %s - 1) "
                "{ %s %s = %s[%s]; %s}" % (idx, name, idx, idx, idx, elem,
                                          var, name, idx, body))
        text = text[:m.start()] + loop + text[end:]
    text = text.replace("foreach_", "foreach")

    # Plain members.
    def sub(pat, fn):
        nonlocal text
        scan = cs2cpp._blank(text)
        out, last = [], 0
        for m in re.finditer(pat, scan):
            if m.start() < last:
                continue
            r = fn(m, scan)
            if r is None:
                continue
            a, b, rep = r
            out.append(text[last:a])
            out.append(rep)
            last = b
        out.append(text[last:])
        text = "".join(out)

    def simple(m, scan):
        name, member = m.group(1), m.group(2)
        kind = decls[name][0]
        op = scan.index("(", m.end() - 1)
        cp = _match(scan, op, "(", ")")
        if cp is None:
            return None
        args = text[op + 1:cp].strip()
        if kind == "Stack" and member == "Push":
            return m.start(), cp + 1, "%s.Add(%s)" % (name, args)
        if kind == "Queue" and member == "Enqueue":
            return m.start(), cp + 1, "%s.Add(%s)" % (name, args)
        if member == "Peek" and not args:
            if kind == "Stack":
                return m.start(), cp + 1, "%s[%s.Count - 1]" % (name, name)
            if kind == "Queue":
                return m.start(), cp + 1, "%s[0]" % name
        return None
    sub(recv + r"\s*\.\s*(Push|Enqueue|Peek)\s*\(", simple)

    # Taking members, hoisted before their statement.
    for _pass in range(256):
        scan = cs2cpp._blank(text)
        found = None
        for m in re.finditer(recv + r"\s*\.\s*(Pop|Dequeue|Add|UnionWith|"
                             r"IntersectWith|ExceptWith)\s*\(", scan):
            name, member = m.group(1), m.group(2)
            kind, elem = decls[name]
            if (member in ("Pop",) and kind != "Stack") or (
                    member == "Dequeue" and kind != "Queue") or (
                    member in ("Add", "UnionWith", "IntersectWith",
                               "ExceptWith") and kind != "HashSet"):
                continue
            if _header_of(scan, m.start()):
                continue                  # runs each time round: not ours
            found = m
            break
        if not found:
            break
        m = found
        name, member = m.group(1), m.group(2)
        kind, elem = decls[name]
        op = scan.index("(", m.end() - 1)
        cp = _match(scan, op, "(", ")")
        s0 = _statement_start(scan, m.start())
        head = isinstance(s0, tuple)
        if head:
            s0 = s0[1]
        s1 = None if s0 is None or head else _statement_end(scan, s0)
        if cp is None or s0 is None or (s1 is None and not head):
            # mark past it so the loop moves on
            text = text[:m.start(2)] + "_" + text[m.start(2):]
            continue
        args = text[op + 1:cp].strip()
        whole = (not head) and re.sub(r"\s", "", scan[s0:s1]) == re.sub(
            r"\s", "", scan[m.start():cp + 1])
        pre, value = "", None
        if member in ("Pop", "Dequeue"):
            t = fresh("take")
            at = ("%s.Count - 1" % name) if member == "Pop" else "0"
            pre = ('_cs_require_nonempty(%s.Count, "%s"); %s %s = %s[%s]; '
                   "%s.RemoveAt(%s); " % (name, kind, elem, t, name, at,
                                          name, at))
            value = t
        elif member == "Add":
            # `.Add@(` until the end: the rewrite's own Add is not another
            # HashSet.Add to rewrite.
            if whole:
                rep = "if (!%s.Contains(%s)) %s.Add@(%s);" % (
                    name, args, name, args)
                text = text[:s0] + rep + text[s1 + 1:]
                continue
            t = fresh("added")
            v = fresh("v")
            pre = ("%s %s = %s; bool %s = !%s.Contains(%s); if (%s) %s.Add@(%s); "
                   % (elem, v, args, t, name, v, t, name, v))
            value = t
        else:
            k = fresh("k")
            if member == "UnionWith":
                loop = ("for (int %s = 0; %s < %s.Count; %s = %s + 1) "
                        "if (!%s.Contains(%s[%s])) %s.Add@(%s[%s]);"
                        % (k, k, args, k, k, name, args, k, name, args, k))
            else:
                neg = "!" if member == "IntersectWith" else ""
                loop = ("for (int %s = %s.Count - 1; %s >= 0; %s = %s - 1) "
                        "if (%s%s.Contains(%s[%s])) %s.RemoveAt(%s);"
                        % (k, name, k, k, k, neg, args, name, k, name, k))
            if not whole:
                text = text[:m.start(2)] + "_" + text[m.start(2):]
                continue
            text = text[:s0] + loop + text[s1 + 1:]
            continue
        if head:
            # before the `if` / `switch`, its condition reading the value
            text = text[:s0] + pre + text[s0:m.start()] + value + text[cp + 1:]
            continue
        stmt = text[s0:m.start()] + value + text[cp + 1:s1 + 1]
        if whole:
            rep = pre                     # the value is not used
        elif re.match(r"(?:var|[\w.<>\[\],]+)\s+\w+\s*=", scan[s0:s1]):
            rep = pre + stmt              # a declaration keeps its scope
        else:
            rep = "{ %s%s }" % (pre, stmt)
        text = text[:s0] + rep + text[s1 + 1:]
    text = text.replace(".Add@(", ".Add(")
    # restore any member marked past
    text = re.sub(r"\.\s*_(Pop|Dequeue|Add|UnionWith|IntersectWith|ExceptWith)"
                  r"\s*\(", lambda m: "." + m.group(1) + "(", text)

    # The types, last: every rewrite above read them.
    text = cs2cpp.code_sub(r"(?<![\w.])%s(?:%s|List)(\s*<)" % (
        _Q, "|".join(_KINDS)), lambda m: "List" + m.group(1), text)
    return text


# ---------------------------------------------------------------------------
# Multidimensional arrays: T[,] (and T[,,]) as a List<T> and its dimensions
# ---------------------------------------------------------------------------

_DEFAULTS = {"int": "0", "short": "0", "byte": "0", "long": "0", "uint": "0",
             "float": "0f", "double": "0f", "bool": "false", "string": '""'}


def _class_body_open(scan, k):
    """The `{` of the class whose member declaration is at k, or None when
    k is inside a method (the nearest enclosing brace is not a class's)."""
    depth = 0
    j = k - 1
    while j >= 0:
        c = scan[j]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                head = scan[max(0, j - 200):j]
                head = head[max(head.rfind(";"), head.rfind("}"),
                                head.rfind("{")) + 1:]
                if re.search(r"(?<![\w])(?:class|struct)\s+\w+", head):
                    return j
                return None
            depth -= 1
        j -= 1
    return None


def desugar_multidim(text):
    """`T[,] g = new T[a, b];` and its uses, as a `List<T>` `g` (row-major,
    as .NET lays one out) with `_cs_g_d0`, `_cs_g_d1`:

        new T[a, b]        the dimensions stored, the list filled with
                           default(T)
        g[x, y]            g[_cs_idx2(x, _cs_g_d0, y, _cs_g_d1)] -- each
                           index checked: IndexOutOfRangeException
        g.GetLength(k)     _cs_g_dk
        g.Length, g.Rank   g.Count, the rank
        foreach            the list's, row-major

    A field with an initializer is filled at the start of `Awake` (made if
    the class has none). Elements: int, float, bool, string; an array
    literal (`{ {1, 2}, .. }`) or a `T[,]` parameter is left for the stub
    check. Every line stays on its line."""
    scan = cs2cpp._blank(text)
    decl = re.compile(r"(?<![\w.])(int|short|byte|long|uint|float|double|bool|"
                      r"string)\s*\[\s*((?:,\s*)+)\]\s+(\w+)")
    arrays = {}                           # name -> (elem, rank, is_field)
    for m in decl.finditer(scan):
        rank = m.group(2).count(",") + 1
        is_field = _class_body_open(scan, m.start()) is not None
        arrays[m.group(3)] = (m.group(1), rank, is_field)
    if not arrays:
        return text
    alt = "|".join(re.escape(n) for n in sorted(arrays, key=len, reverse=True))

    def dims(name):
        return ["_cs_%s_d%d" % (name, d) for d in range(arrays[name][1])]

    def fill(name, sizes):
        elem = arrays[name][0]
        ds = dims(name)
        sets = " ".join("%s = %s;" % (d, sz) for d, sz in zip(ds, sizes))
        total = " * ".join(ds)
        return ("%s.Clear(); %s for (int _cs_k = 0; _cs_k < %s; _cs_k = "
                "_cs_k + 1) %s.Add(%s);" % (name, sets, total, name,
                                            _DEFAULTS.get(elem, "0")))

    awake_fills = {}                      # class `{` index -> fills
    dim_fields = {}                       # class `{` index -> declarations
    # Declarations (fields and locals), with or without `= new T[..]`.
    for _pass in range(64):
        scan = cs2cpp._blank(text)
        m = re.search(r"(?<![\w.])((?:(?:public|private|protected|internal|"
                      r"static|readonly)\s+)*)(int|short|byte|long|uint|float|"
                      r"double|bool|string)\s*\[\s*((?:,\s*)+)\]\s+(%s)\s*"
                      r"(=\s*new\s+\w+\s*\[([^\]]*)\]\s*)?;" % alt, scan)
        if not m:
            break
        mods, elem, name = m.group(1) or "", m.group(2), m.group(4)
        sizes = ([x.strip() for x in cs2cpp.split_call_args(
            text[m.start(6):m.end(6)])] if m.group(5) else None)
        ds = dims(name)
        is_field = arrays[name][2]
        if is_field:
            # private: Unity would not serialize a multidimensional array.
            # The dimensions go at the class's end, each on its own line
            # (the analysis finds a field at a line's start).
            rep = "%sList<%s> %s = new List<%s>();" % (
                mods.replace("public", "private"), elem, name, elem)
            ob = _class_body_open(scan, m.start())
            dim_fields.setdefault(ob, []).extend(
                "private int %s;" % d for d in ds)
            if sizes:
                awake_fills.setdefault(ob, []).append(fill(name, sizes))
        else:
            rep = "List<%s> %s = new List<%s>(); %s" % (
                elem, name, elem, " ".join("int %s = 0;" % d for d in ds))
            if sizes:
                rep += " " + fill(name, sizes)
        text = text[:m.start()] + rep + text[m.end():]
    # Assignments of a new array.
    for _pass in range(64):
        scan = cs2cpp._blank(text)
        m = re.search(r"(?<![\w.])(?:this\s*\.\s*)?(%s)\s*=\s*new\s+\w+\s*\[([^\]]*)\]"
                      r"\s*;" % alt, scan)
        if not m:
            break
        sizes = [x.strip() for x in cs2cpp.split_call_args(
            text[m.start(2):m.end(2)])]
        text = text[:m.start()] + "{ " + fill(m.group(1), sizes) + " }" \
            + text[m.end():]
    # Element access, lengths.
    for _pass in range(256):
        scan = cs2cpp._blank(text)
        m = re.search(r"(?<![\w.])(?:this\s*\.\s*)?(%s)\s*\[" % alt, scan)
        found = False
        while m:
            ob = m.end() - 1
            cb = _match(scan, ob, "[", "]")
            inner = text[ob + 1:cb] if cb is not None else ""
            idx = [x.strip() for x in cs2cpp.split_call_args(inner)]
            name = m.group(1)
            if cb is not None and len(idx) == arrays[name][1] > 1:
                ds = dims(name)
                args = ", ".join("(%s), %s" % (i, d) for i, d in zip(idx, ds))
                rep = "%s[_cs_idx%d(%s)]" % (name, len(idx), args)
                text = text[:m.start()] + rep + text[cb + 1:]
                found = True
                break
            m = re.compile(r"(?<![\w.])(?:this\s*\.\s*)?(%s)\s*\[" % alt).search(
                scan, m.end())
        if not found:
            break
    text = cs2cpp.code_sub(
        r"(?<![\w.])(%s)\s*\.\s*GetLength\s*\(\s*(\d)\s*\)" % alt,
        lambda m: "_cs_%s_d%s" % (m.group(1), m.group(2)), text)
    text = cs2cpp.code_sub(r"(?<![\w.])(%s)\s*\.\s*Length\b" % alt,
                           lambda m: "%s.Count" % m.group(1), text)
    text = cs2cpp.code_sub(r"(?<![\w.])(%s)\s*\.\s*Rank\b" % alt,
                           lambda m: str(arrays[m.group(1)][1]), text)
    # Field initializers: filled at the start of Awake; dimension fields
    # (and an Awake the class lacked) after the class's last line.
    for ob in sorted(set(awake_fills) | set(dim_fields), reverse=True):
        scan = cs2cpp._blank(text)
        cb = _match(scan, ob, "{", "}")
        body = scan[ob:cb]
        am = re.search(r"(?<![\w.])void\s+Awake\s*\(\s*\)\s*\{", body)
        fills = " ".join(awake_fills.get(ob, []))
        tail = list(dim_fields.get(ob, []))
        if fills and am:
            at = ob + am.end()
            text = text[:at] + " " + fills + text[at:]
            cb += len(fills) + 1
        elif fills:
            tail.append("void Awake() { " + fills + " }")
        if tail:
            text = text[:cb].rstrip(" \t") + "\n" + "\n".join(
                " " + t for t in tail) + "\n" + text[cb:]
    return text


def desugar_list_foreach(text, counter=None):
    """`foreach (T v in xs) body` over a local `List<T>` (one a Queue,
    HashSet or T[,] became too) as an index loop -- `{ T v = xs[k]; body }`
    -- which the packed list lowering has; it had no `foreach` over a local.
    A field's list keeps the path it had."""
    counter = counter if counter is not None else [0]
    scan = cs2cpp._blank(text)
    locals_ = {}
    for m in re.finditer(r"(?<![\w.])List\s*<\s*([^<>;]+?)\s*>\s+(\w+)\s*[=;]",
                         scan):
        if _class_body_open(scan, m.start()) is None:
            locals_[m.group(2)] = text[m.start(1):m.end(1)].strip()
    if not locals_:
        return text
    alt = "|".join(re.escape(n) for n in sorted(locals_, key=len, reverse=True))
    for _pass in range(64):
        scan = cs2cpp._blank(text)
        m = re.search(r"(?<![\w.])foreach\s*\(\s*(var|[\w.<>\[\]]+)\s+(\w+)\s+in\s+"
                      r"(%s)\s*\)" % alt, scan)
        if not m:
            break
        ty = m.group(1)
        if ty == "var":
            ty = locals_[m.group(3)]
        var, name = m.group(2), m.group(3)
        k = m.end()
        while k < len(scan) and scan[k] in " \t\r\n":
            k += 1
        if k < len(scan) and scan[k] == "{":
            e = _match(scan, k, "{", "}")
            body = text[k + 1:e]
            end = e + 1
        else:
            e = _statement_end(scan, k)
            if e is None:
                break
            body = text[k:e + 1]
            end = e + 1
        counter[0] += 1
        idx = "_cs_f%d" % counter[0]
        loop = ("for (int %s = 0; %s < %s.Count; %s = %s + 1) "
                "{ %s %s = %s[%s]; %s}" % (idx, idx, name, idx, idx, ty, var,
                                          name, idx, body))
        text = text[:m.start()] + loop + text[end:]
    return text
