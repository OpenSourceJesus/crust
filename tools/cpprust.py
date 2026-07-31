"""cpprust -- a minimal C++ subset, lowered to C.

`#include "foo.cpp"` translates a small C++ dialect in place, the same way
`#include "foo.py"` handles rpython. What the subset buys, and why it is worth
having next to Rust rather than instead of it, is one thing: **destructors**.

Crust has no `Drop`. Scope exit cannot run code, so every allocating type in
its core carries an explicit `free_buf` and the caller has to remember. C++ is
the one of the three languages here whose object model is built around
deterministic destruction, so a C++ class is the natural place to put an RAII
wrapper around a Rust type -- the destructor call is emitted at scope exit, and
the Rust side keeps its explicit API for callers that want it.

The subset, deliberately small:

  * `class` / `struct` with data members and methods
  * constructors and a destructor: a local `Type name(args);` becomes
    `Type_new` at the declaration and `Type_drop` at the closing `}` of the
    enclosing block (inside the `.cpp` only -- the include hook never sees
    the C TU that pulled the file in)
  * `public:` / `private:` / `protected:` labels (parsed, not enforced --
    access control is a compile-time property and this is a lowering, not a
    type checker; pretending to enforce it would be worse than not claiming to)
  * `template<typename T>` on classes, monomorphised on use

Drops run at `}` only; an early `return` does not insert destructor calls, so
a non-void helper should nest the guarded locals in an inner block and return
after that block closes. Call `_drop` explicitly when that shape does not fit.

Not supported, and reported rather than mistranslated: inheritance, virtual
functions, exceptions, operator overloading, `new`/`delete`, the STL. A `virtual`
keyword is an error, not a silently-ignored token, because a program that
expects dynamic dispatch and gets static is wrong in a way that will not
surface until it matters.

The lowering is the same shape Crust uses for `impl` blocks: a method becomes
`Class_method(Class *this, ..)`, a template becomes one struct per
instantiation. That is not a coincidence -- it means a C++ class and a Rust
`impl` over the same data produce the same C, so the two can be mixed in one
unit without a shim.
"""

import os
import re


class CppError(Exception):
    """A C++ subset translation error."""

    def __init__(self, message):
        self.args = (message,)
        self.message = message


_UNSUPPORTED = ("virtual", "throw", "try", "catch", "operator", "new",
                "delete", "dynamic_cast", "typeid")

# `template<typename T>` / `template<class T>`, one parameter.
_TEMPLATE = re.compile(r"\btemplate\s*<\s*(?:typename|class)\s+(\w+)\s*>")


def _strip_comments(text):
    """Blank comments, preserving newlines so line numbers hold."""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(c if c == "\n" else " " for c in text[i:j]))
            i = j
        elif text[i] in "\"'":
            q = text[i]
            j = i + 1
            while j < n and text[j] != q:
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(text[i:j])
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _match_brace(text, open_idx):
    """Index of the `}` closing the `{` at `open_idx`, or None."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _check_unsupported(scan, path):
    for kw in _UNSUPPORTED:
        m = re.search(r"\b%s\b" % kw, scan)
        if m:
            line = scan.count("\n", 0, m.start()) + 1
            raise CppError(
                "%s:%d: `%s` is not in the C++ subset. Supported: classes, "
                "constructors, destructors, and single-parameter templates."
                % (os.path.basename(path), line, kw))


class Member(object):
    __slots__ = ("kind", "ret", "name", "params", "body", "line")

    def __init__(self, kind, ret, name, params, body, line):
        self.kind = kind          # "field" | "method" | "ctor" | "dtor"
        self.ret = ret
        self.name = name
        self.params = params
        self.body = body
        self.line = line


class Class(object):
    __slots__ = ("name", "tparam", "members", "line")

    def __init__(self, name, tparam, members, line):
        self.name = name
        self.tparam = tparam      # template parameter, or None
        self.members = members
        self.line = line


_ACCESS = re.compile(r"\b(public|private|protected)\s*:")


def _split_members(body, cname, line0):
    """Parse a class body into fields, methods, a constructor and destructor."""
    body = _ACCESS.sub("", body)
    members = []
    i, n = 0, len(body)
    while i < n:
        while i < n and body[i] in " \t\r\n;":
            i += 1
        if i >= n:
            break
        start = i
        # A member is `~name(..)`, `name(..)`, or `type name(..)` / `type name;`
        brace = body.find("{", i)
        semi = body.find(";", i)
        if semi >= 0 and (brace < 0 or semi < brace):
            decl = body[i:semi].strip()
            i = semi + 1
            if not decl:
                continue
            parts = decl.replace("*", " * ").split()
            if len(parts) < 2:
                raise CppError("cannot parse member %r in class %s"
                               % (decl, cname))
            members.append(Member("field", " ".join(parts[:-1]), parts[-1],
                                  None, None, line0))
            continue
        if brace < 0:
            break
        head = body[start:brace].strip()
        close = _match_brace(body, brace)
        if close is None:
            raise CppError("unterminated method body in class %s" % cname)
        inner = body[brace + 1:close]
        i = close + 1
        op = head.find("(")
        cp = head.rfind(")")
        if op < 0 or cp < op:
            raise CppError("cannot parse member %r in class %s" % (head, cname))
        params = head[op + 1:cp].strip()
        sig = head[:op].strip()
        if sig == "~" + cname:
            members.append(Member("dtor", "void", cname, params, inner, line0))
        elif sig == cname:
            members.append(Member("ctor", "void", cname, params, inner, line0))
        else:
            bits = sig.replace("*", " * ").split()
            if len(bits) < 2:
                raise CppError("cannot parse method %r in class %s"
                               % (head, cname))
            members.append(Member("method", " ".join(bits[:-1]), bits[-1],
                                  params, inner, line0))
    return members


def _find_classes(scan, text):
    """Locate `class`/`struct` definitions with bodies, template-aware."""
    classes = []
    for m in re.finditer(r"\b(class|struct)\s+(\w+)\s*\{", scan):
        open_idx = scan.index("{", m.start())
        close = _match_brace(scan, open_idx)
        if close is None:
            raise CppError("unterminated class %s" % m.group(2))
        # A `template<..>` immediately before makes this a template class.
        tparam = None
        head = scan[:m.start()]
        tm = None
        for tm in _TEMPLATE.finditer(head):
            pass
        if tm is not None and not head[tm.end():].strip():
            tparam = tm.group(1)
        classes.append((m.start(), close + 1,
                        Class(m.group(2), tparam,
                              _split_members(text[open_idx + 1:close],
                                             m.group(2),
                                             scan.count("\n", 0, m.start()) + 1),
                              scan.count("\n", 0, m.start()) + 1)))
    return classes


def _subst_type(text, tparam, concrete):
    if not tparam:
        return text
    return re.sub(r"\b%s\b" % re.escape(tparam), concrete, text)


def _mangle(name):
    return re.sub(r"\W+", "_", name).strip("_")


def _emit_class(cls, targ=None):
    """Emit a class as a C struct plus free functions."""
    sub = (lambda s: _subst_type(s, cls.tparam, targ)) if targ else (lambda s: s)
    cname = cls.name if targ is None else "%s_%s" % (cls.name, _mangle(targ))
    out = ["struct %s;" % cname, "typedef struct %s %s;" % (cname, cname)]
    fields = [m for m in cls.members if m.kind == "field"]
    body = " ".join("%s %s;" % (sub(f.ret), f.name) for f in fields) or \
        "char _cpp_empty;"
    out.append("struct %s { %s };" % (cname, body))

    for m in cls.members:
        if m.kind == "field":
            continue
        params = sub(m.params or "").strip()
        # `this` is a pointer, exactly as an `impl` method's `self` is.
        arglist = "%s *this" % cname + (", " + params if params else "")
        mname = "%s_%s" % (cname, m.name if m.kind == "method" else
                           ("new" if m.kind == "ctor" else "drop"))
        ret = sub(m.ret)
        inner = sub(m.body or "")
        # Bare member names inside a body refer to fields; qualify them.
        for f in fields:
            inner = re.sub(r"(?<![\w.>])%s\b" % re.escape(f.name),
                           "this->" + f.name, inner)
        inner = inner.replace("this->this->", "this->")
        out.append("static %s %s(%s) {%s}" % (ret, mname, arglist, inner))
    has_ctor = any(m.kind == "ctor" for m in cls.members)
    has_dtor = any(m.kind == "dtor" for m in cls.members)
    return out, cname, has_ctor, has_dtor


def _prev_word(text, idx):
    """Word immediately before `idx`, skipping whitespace."""
    j = idx - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return ""
    end = j + 1
    while j >= 0 and (text[j].isalnum() or text[j] == "_"):
        j -= 1
    return text[j + 1:end]


def _rewrite_scopes(text, type_info):
    """Emit ctor calls at local decls and dtor calls at each closing `}`.

    `type_info` maps mangled class name -> {"ctor": bool, "dtor": bool}.
    Only by-value locals inside a block are rewritten; file-scope decls,
    pointers, and `struct`/`typedef` forms are left alone. Early `return`
    does not insert drops -- that is a known bound on this pass.
    """
    if not type_info:
        return text
    names = sorted(type_info, key=len, reverse=True)
    type_alt = "|".join(re.escape(n) for n in names)
    # `Type name;` or `Type name(args);` -- not `Type *p` (star between).
    decl_re = re.compile(
        r"(?<![\w.])(%s)\s+(\w+)\s*(?:\(([^;]*)\))?\s*;" % type_alt)

    out = []
    scopes = [[]]          # stack of live (ctype, vname) lists
    i, n = 0, len(text)
    in_str = None
    while i < n:
        c = text[i]
        if in_str is not None:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in "\"'":
            in_str = c
            out.append(c)
            i += 1
            continue
        if c == "{":
            scopes.append([])
            out.append(c)
            i += 1
            continue
        if c == "}":
            live = scopes.pop() if len(scopes) > 1 else []
            for ctype, vname in reversed(live):
                out.append("%s_drop(&%s); " % (ctype, vname))
            if not scopes:
                scopes = [[]]
            out.append(c)
            i += 1
            continue

        m = decl_re.match(text, i)
        if m and _prev_word(text, i) not in ("struct", "typedef", "union"):
            ctype, vname, args = m.group(1), m.group(2), m.group(3)
            info = type_info[ctype]
            # File-scope: leave the spelling alone (no automatic Drop).
            if len(scopes) <= 1 or not info["ctor"]:
                out.append(m.group(0))
                i = m.end()
                continue
            out.append("%s %s; " % (ctype, vname))
            if args is None or not args.strip():
                out.append("%s_new(&%s);" % (ctype, vname))
            else:
                out.append("%s_new(&%s, %s);" % (ctype, vname, args.strip()))
            if info["dtor"]:
                scopes[-1].append((ctype, vname))
            i = m.end()
            continue

        out.append(c)
        i += 1
    return "".join(out)


def translate(text, path="<cpp>"):
    """Translate a C++ subset source to C. Raises CppError on anything else."""
    scan = _strip_comments(text)
    _check_unsupported(scan, path)

    classes = _find_classes(scan, text)
    if not classes:
        return text

    # Which template instantiations does the rest of the file ask for?
    wanted = {}
    for _s, _e, cls in classes:
        if not cls.tparam:
            continue
        for m in re.finditer(r"\b%s\s*<\s*([\w ]+?)\s*>" % re.escape(cls.name),
                             scan):
            wanted.setdefault(cls.name, set()).add(m.group(1).strip())

    pieces = []
    type_info = {}
    prev = 0
    for start, end, cls in classes:
        # Keep everything before the class, minus any `template<..>` header,
        # which has no C equivalent.
        head = text[prev:start]
        head = _TEMPLATE.sub("", head)
        pieces.append(head)
        if cls.tparam:
            for targ in sorted(wanted.get(cls.name, ())):
                emitted, cname, has_ctor, has_dtor = _emit_class(cls, targ)
                pieces.append("\n".join(emitted))
                type_info[cname] = {"ctor": has_ctor, "dtor": has_dtor}
        else:
            emitted, cname, has_ctor, has_dtor = _emit_class(cls)
            pieces.append("\n".join(emitted))
            type_info[cname] = {"ctor": has_ctor, "dtor": has_dtor}
        prev = end
    pieces.append(text[prev:])
    out = "".join(pieces)

    # Rewrite uses: `Ring<int> r;` -> `Ring_int r;`, `r.push(1)` is left to the
    # caller (see the README) since C has no method syntax.
    for _s, _e, cls in classes:
        if not cls.tparam:
            continue
        for targ in sorted(wanted.get(cls.name, ())):
            out = re.sub(r"\b%s\s*<\s*%s\s*>" % (re.escape(cls.name),
                                                 re.escape(targ)),
                         "%s_%s" % (cls.name, _mangle(targ)), out)

    return _rewrite_scopes(out, type_info)
