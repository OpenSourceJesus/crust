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

_KINDS = ("Stack", "Queue", "HashSet", "LinkedList")
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
        r"(?<![\w.])var\s+(\w+)(\s*=\s*new\s+%s(?:LinkedList|List|Stack|Queue|"
        r"HashSet)\s*<\s*([^<>;]+?)\s*>)" % _Q,
        lambda m: "%s<%s> %s%s" % (
            re.search(r"(?<![\w])(LinkedList|List|Stack|Queue|HashSet)\s*<",
                      m.group(2)).group(1),
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

    # LinkedList: ends by value; node references are left for the stub check.
    def ll_call(m, scan):
        name, member = m.group(1), m.group(2)
        if decls[name][0] != "LinkedList":
            return None
        op = scan.index("(", m.end() - 1)
        cp = _match(scan, op, "(", ")")
        if cp is None or not re.match(r"\s*;", scan[cp + 1:]):
            return None                   # used as a value: a node, not ours
        args = text[op + 1:cp].strip()
        if member == "AddLast" and args:
            return m.start(), cp + 1, "%s.Add(%s)" % (name, args)
        if member == "AddFirst" and args:
            return m.start(), cp + 1, "%s.Insert(0, %s)" % (name, args)
        if member == "RemoveFirst" and not args:
            return m.start(), cp + 1, "%s.RemoveAt(_cs_ll_first(%s.Count))" % (
                name, name)
        if member == "RemoveLast" and not args:
            return m.start(), cp + 1, "%s.RemoveAt(_cs_ll_last(%s.Count))" % (
                name, name)
        return None
    sub(recv + r"\s*\.\s*(AddLast|AddFirst|RemoveFirst|RemoveLast)\s*\(",
        ll_call)

    def ll_end(m, scan):
        name, which = m.group(1), m.group(2)
        if decls[name][0] != "LinkedList":
            return None
        idx = "_cs_ll_%s(%s.Count)" % ("first" if which == "First" else "last",
                                       name)
        return m.start(), m.end(), "%s[%s]" % (name, idx)
    sub(recv + r"\s*\.\s*(First|Last)\s*\.\s*Value\b", ll_end)

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


# ---------------------------------------------------------------------------
# byte[] as a List<byte>, where a file does more with bytes than File I/O
# ---------------------------------------------------------------------------

#: What makes a file's `byte[]` a List<byte>: bytes built, converted or
#: hashed. A file whose bytes only go to and from File.ReadAllBytes /
#: WriteAllBytes keeps the packer's ByteArray view, as it had.
_BYTES_TRIGGER = re.compile(
    r"\bEncoding\s*\.|\bConvert\s*\.\s*(?:To|From)Base64String\b|"
    r"\b(?:MD5|SHA256)\b|\bBitConverter\b|\bnew\s+byte\s*\[\s*[^\]\s]")
_ENC = r"(?:System\s*\.\s*Text\s*\.\s*)?Encoding\s*\.\s*(?:UTF8|ASCII|Default)"
_CONV = r"(?:System\s*\.\s*)?Convert"
_HASHNS = r"(?:System\s*\.\s*Security\s*\.\s*Cryptography\s*\.\s*)?"


def desugar_bytes(text):
    """`byte[]` locals and fields as a `List<byte>`, with .NET's byte APIs
    as runtime helpers over it (tools/unity_pack_runtime.py):

        byte[] b = new byte[n];          filled with 0; `{ .. }` added
        b.Length                         b.Count
        Encoding.UTF8.GetBytes(s)        _cs_bytes_utf8(s)    (.GetString)
        Convert.ToBase64String(b)        _cs_bytes_base64(b)  (coost)
        Convert.FromBase64String(s)      _cs_bytes_unbase64(s)
        md5.ComputeHash(b)               _cs_bytes_md5(b)     (coost), from
          `var md5 = MD5.Create()` (also in a `using`); MD5.HashData(b);
          SHA256 alike
        BitConverter.ToString(b)         "AB-CD-.."
        File.ReadAllBytes / WriteAllBytes  over the list, in such a file
    """
    scan = cs2cpp._blank(text)
    if not _BYTES_TRIGGER.search(scan) or not re.search(r"\bbyte\s*\[", scan):
        if not re.search(r"\b(?:MD5|SHA256)\s*\.\s*(?:Create|HashData)\b|"
                         r"\bEncoding\s*\.|\bConvert\s*\.\s*(?:To|From)"
                         r"Base64String\b", scan):
            return text
    # hashers: `var md5 = MD5.Create();` (in a `using (..)` too) goes; its
    # ComputeHash is the digest
    hashers = {}
    for m in re.finditer(r"(?<![\w.])(?:var|%s(MD5|SHA256))\s+(\w+)\s*=\s*"
                         % _HASHNS + r"%s(MD5|SHA256)\s*\.\s*Create\s*\(\s*\)"
                         % _HASHNS, scan):
        hashers[m.group(2)] = m.group(3)
    text = re.sub(r"(?<![\w.])using\s*\(\s*((?:var|%s(?:MD5|SHA256))\s+\w+\s*=\s*"
                  r"%s(?:MD5|SHA256)\s*\.\s*Create\s*\(\s*\))\s*\)"
                  % (_HASHNS, _HASHNS), lambda m: "", text)
    text = re.sub(r"(?<![\w.])(?:var|%s(?:MD5|SHA256))\s+\w+\s*=\s*%s(?:MD5|"
                  r"SHA256)\s*\.\s*Create\s*\(\s*\)\s*;" % (_HASHNS, _HASHNS),
                  "", text)
    for name, kind in hashers.items():
        text = cs2cpp.code_sub(
            r"(?<![\w.])%s\s*\.\s*ComputeHash\s*\(" % re.escape(name),
            "_cs_bytes_%s(" % kind.lower(), text)
    # MD5.Create().ComputeHash(b) inline, and MD5.HashData(b)
    text = cs2cpp.code_sub(r"(?<![\w.])%s(MD5|SHA256)\s*\.\s*Create\s*\(\s*\)"
                           r"\s*\.\s*ComputeHash\s*\(" % _HASHNS,
                           lambda m: "_cs_bytes_%s(" % m.group(1).lower(), text)
    text = cs2cpp.code_sub(r"(?<![\w.])%s(MD5|SHA256)\s*\.\s*HashData\s*\("
                           % _HASHNS,
                           lambda m: "_cs_bytes_%s(" % m.group(1).lower(), text)
    text = cs2cpp.code_sub(r"(?<![\w.])%s\s*\.\s*GetBytes\s*\(" % _ENC,
                           "_cs_bytes_utf8(", text)
    text = cs2cpp.code_sub(r"(?<![\w.])%s\s*\.\s*GetString\s*\(" % _ENC,
                           "_cs_bytes_to_string(", text)
    text = cs2cpp.code_sub(r"(?<![\w.])%s\s*\.\s*ToBase64String\s*\(" % _CONV,
                           "_cs_bytes_base64(", text)
    text = cs2cpp.code_sub(r"(?<![\w.])%s\s*\.\s*FromBase64String\s*\(" % _CONV,
                           "_cs_bytes_unbase64(", text)
    text = cs2cpp.code_sub(r"(?<![\w.])(?:System\s*\.\s*)?BitConverter\s*\.\s*"
                           r"ToString\s*\(", "_cs_bytes_hex_dash(", text)
    text = cs2cpp.code_sub(r"(?<![\w.])(?:System\s*\.\s*IO\s*\.\s*)?File\s*\.\s*"
                           r"ReadAllBytes\s*\(", "_cs_file_read_bytes(", text)
    text = cs2cpp.code_sub(r"(?<![\w.])(?:System\s*\.\s*IO\s*\.\s*)?File\s*\.\s*"
                           r"WriteAllBytes\s*\(", "_cs_file_write_bytes(", text)
    text = _hoist_byte_producers(text)
    # the arrays
    scan = cs2cpp._blank(text)
    names = set(re.findall(r"(?<![\w.])byte\s*\[\s*\]\s+(\w+)", scan))
    for _pass in range(64):
        scan = cs2cpp._blank(text)
        m = re.search(r"(?<![\w.])byte\s*\[\s*\]\s+(\w+)\s*=\s*new\s+byte\s*\["
                      r"\s*([^\]]*?)\s*\]\s*(\{[^{}]*\})?\s*;", scan)
        if not m:
            break
        name = m.group(1)
        if m.group(3):
            elems = [e.strip() for e in cs2cpp.split_call_args(
                text[m.start(3) + 1:m.end(3) - 1]) if e.strip()]
            fill = " ".join("%s.Add(%s);" % (name, e) for e in elems)
        else:
            fill = ("for (int _cs_k = 0; _cs_k < %s; _cs_k = _cs_k + 1) "
                    "%s.Add(0);" % (text[m.start(2):m.end(2)], name))
        text = (text[:m.start()] + "List<byte> %s = new List<byte>(); %s"
                % (name, fill) + text[m.end():])
    text = cs2cpp.code_sub(r"(?<![\w.])byte\s*\[\s*\](?=\s+\w)", "List<byte>",
                           text)
    if names:
        alt = "|".join(re.escape(n) for n in sorted(names, key=len,
                                                    reverse=True))
        text = cs2cpp.code_sub(r"(?<![\w.])(%s)\s*\.\s*Length\b" % alt,
                               lambda m: "%s.Count" % m.group(1), text)
    # A `byte` local (a list's element, `foreach (byte x in h)`) is an int,
    # as the list holds it.
    text = cs2cpp.code_sub(r"(?<![\w.<])byte(?=\s+[A-Za-z_]\w*\s*(?:[=;,)]|in\b))",
                           "int", text)
    return text



_BYTE_PRODUCERS = ("_cs_bytes_utf8", "_cs_bytes_unbase64", "_cs_bytes_md5",
                   "_cs_bytes_sha256", "_cs_file_read_bytes")
_BYTE_CONSUMERS = ("_cs_bytes_to_string", "_cs_bytes_base64", "_cs_bytes_md5",
                   "_cs_bytes_sha256", "_cs_bytes_hex_dash",
                   "_cs_file_write_bytes")
_HOIST_N = [0]


def _hoist_byte_producers(text):
    """A byte helper taking a list takes it by reference, and a call's
    result has no address: `GetString(FromBase64String(s))` hoists the inner
    call into a `List<byte>` temporary before the statement."""
    prod = "|".join(_BYTE_PRODUCERS)
    cons = "|".join(_BYTE_CONSUMERS)
    for _pass in range(64):
        scan = cs2cpp._blank(text)
        found = None
        for m in re.finditer(r"(?<![\w.])(?:%s)\s*\(" % cons, scan):
            op = m.end() - 1
            cp = _match(scan, op, "(", ")")
            if cp is None:
                continue
            for a in re.finditer(r"(?<![\w.])(?:%s)\s*\(" % prod,
                                 scan[op + 1:cp]):
                a0 = op + 1 + a.start()
                ae = _match(scan, op + 1 + a.end() - 1, "(", ")")
                # only an argument by itself, not inside another expression
                before = scan[op + 1:a0].rstrip()
                if before and not before.endswith(","):
                    continue
                if ae is None:
                    continue
                found = (a0, ae + 1)
                break
            if found:
                break
        if not found:
            break
        a0, a1 = found
        s0 = _statement_start(scan, a0)
        if s0 is None or isinstance(s0, tuple):
            break
        s1 = _statement_end(scan, s0)
        if s1 is None:
            break
        _HOIST_N[0] += 1
        tmp = "_cs_bytes%d" % _HOIST_N[0]
        pre = "byte[] %s = %s; " % (tmp, text[a0:a1])
        stmt = text[s0:a0] + tmp + text[a1:s1 + 1]
        if re.match(r"(?:var|[\w.<>\[\],]+)\s+\w+\s*=", scan[s0:s1]):
            rep = pre + stmt
        else:
            rep = "{ %s%s }" % (pre, stmt)
        text = text[:s0] + rep + text[s1 + 1:]
    return text
