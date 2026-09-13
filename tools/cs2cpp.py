#!/usr/bin/env python3
"""cs2cpp — normalize a C# subset into the C++ subset cpprust already lowers.

Approach D from issue #25: refuse out-of-subset C# in C# terms first, then
emit C++ that `tools/cpprust.py` already understands. No cpprust fork.

    from tools.cs2cpp import normalize
"""

from __future__ import annotations

import re


class CsError(Exception):
    def __init__(self, message, path="<cs>"):
        Exception.__init__(self, message)
        self.message = message
        self.path = path


def _blank(text):
    """Blank string/char literals and comments (space-preserving)."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            out.append(' ')
            i += 1
            while i < n:
                out.append(' ')
                if text[i] == '"' and text[i - 1] != '\\':
                    i += 1
                    break
                i += 1
            continue
        if c == "'":
            out.append(' ')
            i += 1
            while i < n and not (text[i] == "'" and text[i - 1] != '\\'):
                out.append(' ')
                i += 1
            if i < n:
                out.append(' ')
                i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                out.append(' ')
                i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            out.append(' ')
            out.append(' ')
            i += 2
            while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                out.append('\n' if text[i] == '\n' else ' ')
                i += 1
            if i + 1 < n:
                out.append(' ')
                out.append(' ')
                i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _match_brace(text, open_i):
    depth = 0
    i, n = open_i, len(text)
    while i < n:
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


# ----- refusals -------------------------------------------------------------

_REFUSALS = [
    (r'\basync\b', "async/await",
     "needs a state machine and a scheduler; see STACKLESS_CALLS.md"),
    (r'\bawait\b', "async/await",
     "needs a state machine and a scheduler; see STACKLESS_CALLS.md"),
    (r'\byield\s+return\b', "yield return",
     "needs an iterator protocol; write the loop"),
    (r'\bdynamic\b', "dynamic", "no runtime metadata beyond typeof"),
    (r'\bpartial\s+class\b', "partial class", "merge the parts into one class"),
    (r'\bevent\s+', "event",
     "use delegate first; event once multicast has a home"),
    (r'\bstackalloc\b', "stackalloc",
     "use a fixed-size local array or an owning buffer"),
    (r'\bparams\s+', "params", "pass an explicit array"),
    (r'\bchecked\s*\(', "checked", "overflow checks are not in the subset"),
    (r'\bunchecked\s*\(', "unchecked", "overflow checks are not in the subset"),
    (r'\bgoto\s+case\b', "goto case", "use if/else or separate handlers"),
    (r'\bfrom\s+\w+\s+in\b', "LINQ query syntax", "write the loop"),
    (r'\bstatic\s+class\b', "static class",
     "use a namespace of free functions"),
    (r'\bstatic\s+[\w:<>,\s\*\&]+\s+\w+\s*\(\s*this\s+',
     "extension method", "not phase 1; call a free function"),
]


def _check_refusals(text, path):
    scan = _blank(text)
    for pat, name, why in _REFUSALS:
        m = re.search(pat, scan)
        if m:
            line = scan.count('\n', 0, m.start()) + 1
            raise CsError(
                "%s:%d: `%s` is not in the C# subset. %s."
                % (path, line, name, why), path)
    m = re.search(r'\b\w+\s*\[\s*,', scan)
    if m:
        line = scan.count('\n', 0, m.start()) + 1
        raise CsError(
            "%s:%d: multidimensional arrays are not in the C# subset. "
            "Use a jagged array (`T[][]`)." % (path, line), path)
    m = re.search(r'\.\s*(Where|Select|SelectMany|OrderBy|GroupBy)\s*\(', scan)
    if m:
        line = scan.count('\n', 0, m.start()) + 1
        raise CsError(
            "%s:%d: LINQ (`%s`) is not in the C# subset. Write the loop."
            % (path, line, m.group(1)), path)


# ----- class / struct / interface -------------------------------------------

_ACCESS = re.compile(r'(?m)^([ \t]*)(public|private|protected|internal)\s+')


def _rewrite_class_body(body):
    out, section = [], None
    for line in body.split('\n'):
        m = _ACCESS.match(line)
        if m:
            indent, acc = m.group(1), m.group(2)
            rest = line[m.end():]
            label = "public" if acc in ("public", "internal") else acc
            if label != section:
                out.append("%s%s:" % (indent, label))
                section = label
            out.append(indent + rest)
        else:
            out.append(line)
    body = '\n'.join(out)
    # Mid-line / same-line access modifiers (`public int x; public A(){}`)
    # are not section labels — strip them. Negative lookahead keeps `public:`.
    return re.sub(
        r'\b(public|private|protected|internal)\s+(?!:)', '', body)



def _interface_methods_pure(body):
    return re.sub(
        r'(?m)^([ \t]*)([\w:<>,\s\*\&]+)\s+(\w+)\s*\(([^;{]*)\)\s*;',
        lambda m: "%svirtual %s %s(%s) = 0;" % (
            m.group(1), m.group(2).strip(), m.group(3), m.group(4)),
        body,
    )


def _rewrite_types(text, shared_names, struct_names):
    scan = _blank(text)
    matches = list(re.finditer(
        r'(?m)\b(class|struct|interface)\s+(\w+)\s*(:\s*[^{]+)?\{', scan))
    if not matches:
        return text
    out, prev = [], 0
    for m in matches:
        out.append(text[prev:m.start()])
        kind, name = m.group(1), m.group(2)
        bases = m.group(3) or ""
        open_b = m.end() - 1
        close_b = _match_brace(scan, open_b)
        if close_b < 0:
            out.append(text[m.start():])
            return ''.join(out)
        body = _rewrite_class_body(text[open_b + 1:close_b])
        emit = "struct" if kind == "struct" else "class"
        if kind == "interface":
            body = _interface_methods_pure(body)
            emit = "class"
        # Default class: empty dtor → cpprust owning → copy refused.
        if (emit == "class" and kind != "struct"
                and name not in shared_names and name not in struct_names):
            if not re.search(r'~\s*' + re.escape(name) + r'\s*\(', body):
                body = body.rstrip() + "\npublic:\n    ~%s() {}\n" % name
        if bases.strip():
            parts = [p.strip() for p in bases.strip().lstrip(':').split(',')]
            bases = " : " + ", ".join(
                p if p.startswith("public") or p.startswith("private")
                else "public " + p for p in parts if p)
        chunk = "%s %s%s {%s};" % (emit, name, bases, body)
        after = close_b + 1
        while after < len(text) and text[after] in ' \t':
            after += 1
        if after < len(text) and text[after] == ';':
            prev = after + 1
        else:
            prev = close_b + 1
        out.append(chunk)
    out.append(text[prev:])
    return ''.join(out)


# ----- surface --------------------------------------------------------------

def _desugar_auto_properties(text):
    prop_re = re.compile(
        r'(?m)^([ \t]*)(?:(public|private|protected|internal)\s+)?'
        r'([\w:<>,\s\*\&]+)\s+(\w+)\s*\{\s*get\s*;\s*'
        r'(?:(public|private|protected|internal)\s+)?set\s*;\s*\}')
    names = []

    def repl(m):
        indent, _acc, typ, name, _s = m.groups()
        names.append(name)
        field, typ = "_" + name, typ.strip()
        return (
            "%sprivate:\n%s    %s %s;\n%spublic:\n"
            "%s    %s get_%s() { return this->%s; }\n"
            "%s    void set_%s(%s v) { this->%s = v; }\n"
            % (indent, indent, typ, field, indent,
               indent, typ, name, field,
               indent, name, typ, field))

    text = prop_re.sub(repl, text)
    for name in sorted(set(names), key=len, reverse=True):
        text = re.sub(
            r'\.' + re.escape(name) + r'\s*=\s*([^;]+);',
            r'.set_' + name + r'(\1);', text)
        text = re.sub(
            r'\.(?!get_|set_)' + re.escape(name) + r'\b(?!\s*\()',
            r'.get_' + name + r'()', text)
    return text


def _rewrite_surface(text):
    text = re.sub(r'\bthis\.', 'this->', text)
    text = re.sub(r'\bnull\b', 'nullptr', text)
    text = re.sub(r'\bstring\b', 'std::string', text)
    text = re.sub(r'\bvar\b', 'auto', text)
    text = re.sub(
        r'\bforeach\s*\(\s*([^)]+?)\s+in\s+([^)]+)\)',
        r'for (\1 : \2)', text)
    text = re.sub(r'\bList\s*<', 'std::vector<', text)
    text = re.sub(r'\bDictionary\s*<', 'std::map<', text)
    text = re.sub(
        r'\babstract\s+([\w:<>,\s\*\&]+)\s+(\w+)\s*\(([^)]*)\)\s*;',
        r'virtual \1 \2(\3) = 0;', text)
    # C# `override int f()` → C++ `int f() override` (specifier is postfix).
    text = re.sub(
        r'\boverride\s+([\w:<>,\s\*\&]+)\s+(\w+)\s*\(([^)]*)\)',
        r'\1 \2(\3) override', text)
    text = re.sub(r'\babstract\s+class\b', 'class', text)
    text = re.sub(r'\bsealed\s+class\b', 'class', text)
    text = re.sub(r'\bsealed\s+', '', text)
    text = re.sub(r'\bthrow\b', 'raise', text)
    text = re.sub(r'\bcatch\s*\(', 'except (', text)
    text = _desugar_auto_properties(text)
    text = re.sub(
        r'(?m)^([ \t]*[\w:<>,\s\*\&]+\s+\w+\s*\([^)]*\))\s*=>\s*([^;]+);',
        r'\1 { return \2; }', text)
    return text


def _mark_except_functions(text):
    def maybe(m):
        head, body = m.group(1), m.group(2)
        if re.search(r'\braise\b', body) and 'except' not in head:
            return head + " except {" + body + "}"
        return m.group(0)

    return re.sub(
        r'((?:[\w:<>,\s\*\&]+)\s+\w+\s*\([^)]*\)\s*)'
        r'\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}',
        maybe, text)


def _lower_new(text, shared_names):
    # `T a = new T(args);` → `T a(args);` (no copy from temporary; owning
    # classes delete their copy ctor). Shared types use make_shared so a
    # later `new T(` pass cannot wrap the constructor argument again.
    def decl(m):
        typ, name, args = m.group(1), m.group(2), m.group(3).strip()
        if typ in shared_names:
            return ("std::shared_ptr<%s> %s = std::make_shared<%s>(%s);"
                    % (typ, name, typ, args))
        if args:
            return "%s %s(%s);" % (typ, name, args)
        return "%s %s;" % (typ, name)

    text = re.sub(
        r'\b(\w+)\s+(\w+)\s*=\s*new\s+\1\s*\(([^)]*)\)\s*;',
        decl, text)

    def repl(m):
        typ, args = m.group(1), m.group(2).strip()
        if typ in shared_names:
            return "std::make_shared<%s>(%s)" % (typ, args)
        return ("%s(%s)" % (typ, args)) if args else ("%s()" % typ)

    return re.sub(r'\bnew\s+(\w+)\s*\(([^)]*)\)', repl, text)


def _wrap_shared_locals(text, shared_names):
    for name in shared_names:
        # Skip names already rewritten to shared_ptr<T>.
        text = re.sub(
            r'(?<!shared_ptr<)\b' + re.escape(name) + r'\s+(\w+)\s*=',
            'std::shared_ptr<' + name + r'> \1 =', text)
        text = re.sub(
            r'(?<!shared_ptr<)\b' + re.escape(name) + r'\s+(\w+)\s*;',
            'std::shared_ptr<' + name + r'> \1;', text)
    return text


def _shared_calls(text, shared_names):
    """Rewrite method/field use on shared_ptr locals.

    cpprust rewrites `obj.method` only for class values, not through
    `shared_ptr::operator->`. Emitting `Type_method(var.get(), args)` matches
    the form it already accepts, and avoids `var.get()` meaning
    shared_ptr::get when the C# method is also named get.
    """
    var_type = {}
    for name in shared_names:
        for m in re.finditer(
                r'std::shared_ptr<\s*' + re.escape(name) + r'\s*>\s+(\w+)',
                text):
            var_type[m.group(1)] = name
    for v, typ in sorted(var_type.items(), key=lambda kv: -len(kv[0])):
        def meth(m, typ=typ, v=v):
            method, args = m.group(1), m.group(2).strip()
            if args:
                return "%s_%s(%s.get(), %s)" % (typ, method, v, args)
            return "%s_%s(%s.get())" % (typ, method, v)

        text = re.sub(
            r'\b' + re.escape(v) + r'\.(\w+)\s*\(([^)]*)\)', meth, text)
        text = re.sub(
            r'\b' + re.escape(v) + r'\.(\w+)\b(?!\s*\()',
            v + r'.get()->\1', text)
    return text


def _includes(text, shared_names):
    extras = []
    if shared_names and not re.search(r'#include\s*<memory>', text):
        extras.append("#include <memory>")
    if "std::vector<" in text and not re.search(r'#include\s*<vector>', text):
        extras.append("#include <vector>")
    if "std::map<" in text and not re.search(r'#include\s*<map>', text):
        extras.append("#include <map>")
    if "std::string" in text and not re.search(r'#include\s*<string>', text):
        extras.append("#include <string>")
    return ("\n".join(extras) + "\n" + text) if extras else text


def normalize(text, path="<cs>"):
    """C# subset → C++ subset. Raises CsError on refuse."""
    if text is None:
        raise CsError("%s: empty input" % path, path)
    _check_refusals(text, path)

    shared = set()

    def mark(m):
        shared.add(m.group(1))
        return "class " + m.group(1)

    text = re.sub(r'\[\s*Shared\s*\]\s*class\s+(\w+)', mark, text)
    structs = set(re.findall(r'\bstruct\s+(\w+)', text))

    text = _rewrite_surface(text)
    text = _rewrite_types(text, shared, structs)
    text = _lower_new(text, shared)
    text = _wrap_shared_locals(text, shared)
    text = _shared_calls(text, shared)
    text = _mark_except_functions(text)
    text = _includes(text, shared)
    text = re.sub(r'\binternal\b', 'public', text)
    return text


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "<stdin>"
    src = sys.stdin.read() if path == "<stdin>" else open(path).read()
    try:
        sys.stdout.write(normalize(src, path=path))
    except CsError as e:
        sys.stderr.write("cs2cpp: %s\n" % e.message)
        sys.exit(1)
