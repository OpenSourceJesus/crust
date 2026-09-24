#!/usr/bin/env python3
"""cs2cpp -- normalise a C# subset into the C++ subset `cpprust.py` reads.

This is the front half of `csrust.py`. It does not emit C. It rewrites C#
source into the subset of C++ that `tools/cpprust.py` already lowers, and
hands that over unchanged. See CSHARP.md for why the split is here rather
than in a shared core: the two languages overlap most in passes that are
already written and already tested (`auto`, range-`for`, namespace
flattening, monomorphisation, vtables), and a textual pipeline cannot be
parameterised over its own grammar.

The load-bearing consequence is diagnostics. A refusal must be reported
*here*, against C# text, in C# terms -- so every construct outside the
subset is checked before a single character is rewritten. Anything that
reaches `cpprust.py` and fails there is a bug in this file, not a user
error, and it says so.

Two rules hold throughout, both inherited from `cpprust.py`:

  * matching runs against a *blanked* copy -- comments, string literals and
    directive lines replaced by spaces of the same length -- so a keyword
    inside `Log("await the result")` asks for nothing;
  * newlines are never added or removed, so a line number in the generated
    C++ is a line number in the original `.cs`.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cpprust as cpprust                              # noqa: E402


class CsError(Exception):
    """A C# source outside the subset.

    Carries `.message` rather than relying on `str(e)`, for the same reason
    `CppError` does: py2c gives user classes no `__str__`, so formatting the
    exception itself yields `<obj 0x...>` and the diagnostic is lost in the
    self-hosted build, which is where it is hardest to recover.
    """

    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message


def _blank(text):
    """A same-length copy with comments, literals and directives blanked."""
    return cpprust._blank_directives(
        cpprust._blank_strings(cpprust._strip_comments(text)))


def _line_of(text, idx):
    return text.count("\n", 0, idx) + 1


def _at(path, text, idx):
    return "%s:%d: " % (os.path.basename(path), _line_of(text, idx))


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

#: Keyword -> (what it is, why it is not here, what to write instead).
#: Phrased the way `_check_unsupported` in cpprust.py phrases its refusals:
#: name the construct, give the reason, name the replacement. A refusal with
#: no replacement in it is a bug report filed against the user.
_REFUSED = [
    ("async", "`async` marks a method as resumable, which needs the "
              "compiler to split it into a state machine and a scheduler to "
              "run the pieces. Crust has both halves of that story "
              "elsewhere -- see STACKLESS_CALLS.md and BAREMETAL_THREADS.md "
              "-- but they are not wired to this keyword yet. Write the "
              "blocking call."),
    ("await", "`await` resumes a suspended method, and this subset does not "
              "suspend one -- see `async`. Write the blocking call."),
    ("yield", "`yield return` makes a method an iterator: the compiler turns "
              "it into a state machine whose `MoveNext` resumes where the "
              "last one stopped. Same missing piece as `async`. Build a "
              "`List<T>` and return it, or write the loop at the call site."),
    ("dynamic", "`dynamic` defers member lookup to runtime, which needs "
                "metadata describing every type's members in the binary. "
                "Write the type, or use an `interface`."),
    ("event", "`event` is multicast delegate storage with add/remove "
              "accessors. Delegates themselves are on the way (they lower to "
              "function pointers); the multicast list is not. Hold a "
              "`List<T>` of an `interface` and call them in a loop."),
    ("params", "`params` builds an array from a variable argument list at "
               "each call site, so its length is a property of the call and "
               "not of the signature. Take a `List<T>` and pass one."),
    ("stackalloc", "`stackalloc` is a runtime-sized stack allocation. A "
                   "frame here has a size known at compile time -- that is "
                   "what lets the register allocator and the thread "
                   "partitioner see through it. Use a fixed-size array, or "
                   "`new`."),
    ("checked", "`checked` and `unchecked` switch overflow behaviour for a "
                "region. This subset has one behaviour, C's. Test the "
                "operands."),
    ("unchecked", "`unchecked` switches overflow behaviour for a region; "
                  "this subset has one behaviour, C's."),
    ("lock", "`lock` takes a monitor on an object header, which means every "
             "object carries one. Crust's threading model declares threads "
             "to the compiler instead -- see BAREMETAL_THREADS.md. Use the "
             "primitives there."),
    ("decimal", "`decimal` is a 128-bit base-10 float with no hardware "
                "behind it, so it is a software library rather than a type. "
                "Use `double`, or fixed-point over `long`."),
    ("partial", "`partial` splits one type across several files, and this "
                "pass lowers one file. Put the type in one piece."),
    ("goto", "`goto` is not in the subset."),
    ("char", "C#'s `char` is a 16-bit UTF-16 code unit and C's is an 8-bit "
             "byte, so lowering one to the other would silently change what "
             "every string index means. Use `byte` for ASCII, or `string`."),
]

#: `ref`/`out` arguments are a reference in the C++ sense, which cpprust
#: does lower -- but the C# spelling puts the keyword at the *call site*
#: too, and nothing reads it yet. Separated from the table above because
#: the reason is "not yet" rather than "not ever".
_REFUSED_PARAM_MODS = ("ref", "out", "in")


def _check_refusals(text, path):
    """Report anything outside the subset, in C# terms, before rewriting."""
    scan = _blank(text)

    for kw, why in _REFUSED:
        m = re.search(r"(?<![\w.])%s(?![\w])" % re.escape(kw), scan)
        if m:
            raise CsError("%s`%s` is not in the C# subset. %s"
                          % (_at(path, text, m.start()), kw, why))

    m = re.search(r'(?<![\w])\$"', scan)
    if m:
        raise CsError(
            "%s string interpolation (`$\"..\"`) is not in the C# subset "
            "yet: it desugars to a formatting call, and `string` here is "
            "the one cpprust supplies rather than .NET's. Concatenate, or "
            "call the formatter directly."
            % _at(path, text, m.start()))

    m = re.search(r"(?<![\w])(\w+)\s*\[\s*,", scan)
    if m:
        raise CsError(
            "%s`%s[,]` is a multidimensional array, whose element address "
            "needs a stride the declaration does not carry here. Use a "
            "jagged array (`%s[][]`), which is an array of arrays and "
            "lowers directly."
            % (_at(path, text, m.start()), m.group(1), m.group(1)))

    m = re.search(r"(?<![\w])namespace\s+[\w.]+\s*;", scan)
    if m:
        raise CsError(
            "%sa file-scoped namespace (`namespace N;`) has no closing "
            "brace, so its extent is the rest of the file and this pass "
            "reads extents from braces. Write the braced form."
            % _at(path, text, m.start()))

    # `CreateSpan(ref x, 1)` spells `ref` at a call site, and is read whole
    # by `_lower_memory_marshal` -- the one place a call-site `ref` is.
    mods_scan = re.sub(r"MemoryMarshal\s*\.\s*CreateSpan\s*\(\s*ref\b",
                       lambda m: " " * len(m.group(0)), scan)
    for mod in _REFUSED_PARAM_MODS:
        m = re.search(r"\(\s*%s\s+(?=\w)" % mod, mods_scan)
        if m:
            raise CsError(
                "%s`%s` parameters are not in the C# subset yet. The "
                "lowering exists -- cpprust passes a reference as a pointer "
                "-- but the call site spells the keyword too and nothing "
                "reads it. Return the value, or pass a one-field class."
                % (_at(path, text, m.start()), mod))

    m = re.search(r"(?<![\w])from\s+\w+\s+in(?![\w])", scan)
    if m:
        raise CsError(
            "%sLINQ query syntax is not in the C# subset. It is deferred "
            "execution over an iterator protocol, and this subset has "
            "neither. Write the loop."
            % _at(path, text, m.start()))

    m = re.search(r"\?\?|\?\.", scan)
    if m:
        raise CsError(
            "%s`%s` is a null-conditional operator, which is not in the C# "
            "subset yet. Test for null."
            % (_at(path, text, m.start()), m.group(0)))

    m = re.search(r"(?<![\w.])((?:ReadOnly)?Span)\s*<", scan)
    if m:
        raise CsError(
            "%s`%s<T>` is not in the C# subset. A span is a pointer and a "
            "length into storage something else owns, which is what single "
            "ownership rules out. For struct bytes, the supported form copies "
            "them into a `byte[]`: `MemoryMarshal.AsBytes(MemoryMarshal."
            "CreateSpan(ref x, 1)).ToArray()`."
            % (_at(path, text, m.start()), m.group(1)))

    _check_top_level_statements(text, path)


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------

#: Modifiers that carry no meaning once the class is lowered. Access
#: control is one of them: cpprust parses `public:` labels and does not
#: enforce them (its `_ACCESS.sub("", body)`), and C has no access control
#: to enforce it with, so a per-member `public` is dropped the same way.
_DROPPED_MODIFIERS = ("public", "private", "protected", "internal",
                      "sealed", "override", "readonly", "unsafe", "extern",
                      "volatile", "implicit", "explicit")

_KIND = re.compile(r"(?<![\w])(class|struct|interface|enum)\s+(\w+)")


def _strip_modifiers(text):
    """Drop C# declaration modifiers, keeping every column in place.

    Replaced by spaces rather than removed: a member's line *and column*
    still address the original file, which is most of what makes a
    diagnostic from a later pass usable.
    """
    pat = r"(?<![\w])(%s)(?=[\s])" % "|".join(_DROPPED_MODIFIERS)
    return cpprust._sub_code(pat, lambda m: " " * len(m.group(0)), text)


def _find_types(text):
    """Every `class`/`struct`/`interface`/`enum` as (kind, name, open, close).

    Innermost-last, so a caller rewriting from the end backwards never
    invalidates an offset it has not used yet.
    """
    scan = _blank(text)
    out = []
    for m in _KIND.finditer(scan):
        brace = scan.find("{", m.end())
        if brace < 0:
            continue
        # A base clause or type parameter list may sit between the name and
        # the body, but a `;` may not -- that would be a forward
        # declaration, which C# does not have and which this would
        # otherwise pair with the next type's body.
        if ";" in scan[m.end():brace]:
            continue
        close = cpprust._match_brace(scan, brace)
        if close is None:
            continue
        out.append((m.group(1), m.group(2), m.start(), brace, close))
    return out


def _terminate(text):
    """`}` -> `};` at the end of a type body. C# omits the semicolon."""
    for _kind, _name, _start, _brace, close in reversed(_find_types(text)):
        after = text[close + 1:close + 2]
        if after != ";":
            text = text[:close + 1] + ";" + text[close + 1:]
    return text


def _qualify_bases(text):
    """`: B, I` -> `: public B, public I`.

    C# has no access specifier on a base and C++ requires one on a `class`.
    Which base is the layout base is decided by writing order in both
    languages -- C# puts the class first and the interfaces after, which is
    exactly what `_parse_base` wants -- so the order is left alone.
    """
    scan = _blank(text)
    edits = []
    for _kind, name, start, brace, _close in _find_types(text):
        head = scan[start:brace]
        colon = head.find(":")
        if colon < 0:
            continue
        clause = head[colon + 1:]
        # Generic constraints (`where T : IFoo`) are a different colon and
        # belong to the type parameter, not the type.
        if re.search(r"(?<![\w])where(?![\w])", head[:colon]):
            continue
        pos = start + colon + 1
        for part in cpprust._split_top(clause):
            raw = part.strip()
            if not raw:
                continue
            off = clause.find(raw)
            edits.append((pos + off, "public "))
    for at, ins in sorted(edits, reverse=True):
        text = text[:at] + ins + text[at:]
    return text


_MEMBER_DECL = re.compile(
    r"(?<![\w])([A-Za-z_][\w<>,\[\]\s.]*?[\s*&]+)(\w+)\s*\(([^()]*)\)\s*;")


def _lower_interfaces(text, path):
    """`interface I { int F(); }` -> `class I { virtual int F() = 0; };`

    An interface is a class all of whose methods are pure virtual, which is
    exactly the shape cpprust already lowers to a secondary base with a
    vptr of its own. Nothing new is emitted; the spelling changes.
    """
    for kind, name, start, brace, close in reversed(_find_types(text)):
        if kind != "interface":
            continue
        body = text[brace + 1:close]
        bscan = _blank(body)
        if re.search(r"(?<![\w])(\w+)\s*\([^()]*\)\s*\{", bscan):
            raise CsError(
                "%sinterface `%s` has a method with a body. A default "
                "interface method needs the interface to have a vtable of "
                "its own to put it in, and here an interface *is* the "
                "vtable. Move it to a class."
                % (_at(path, text, start), name))
        out, pos = [], 0
        for m in _MEMBER_DECL.finditer(bscan):
            out.append(body[pos:m.start()])
            out.append("virtual %s%s(%s) = 0;"
                       % (body[m.start(1):m.end(1)].strip() + " ",
                          m.group(2), body[m.start(3):m.end(3)]))
            pos = m.end()
        out.append(body[pos:])
        text = (text[:start] + "class" + text[start + len("interface"):brace + 1]
                + "".join(out) + text[close:])
    return text


def _lower_abstract(text, path):
    """`abstract` on a class is dropped; on a method it is `= 0`."""
    scan = _blank(text)
    # The member form first: `abstract int Get();` -> `virtual int Get() = 0;`
    out, pos = [], 0
    for m in re.finditer(
            r"(?<![\w])abstract\s+([^;{}()]*\([^()]*\))\s*;", scan):
        out.append(text[pos:m.start()])
        out.append("virtual %s = 0;" % text[m.start(1):m.end(1)].strip())
        pos = m.end()
    out.append(text[pos:])
    text = "".join(out)
    # Whatever is left is the class form, which carries no information C++
    # needs: a class with a pure virtual is already uninstantiable.
    return cpprust._sub_code(r"(?<![\w])abstract(?=[\s])",
                             lambda m: " " * len(m.group(0)), text)


def _lower_constants(text):
    """A C# `const` member is C++'s `static const`.

    cpprust emits one at file scope rather than in the struct, because C has
    no static data member -- which is the behaviour wanted here, and the
    reason this is a rename rather than a new pass.
    """
    for _kind, _name, _start, brace, close in reversed(_find_types(text)):
        body = text[brace + 1:close]
        body = cpprust._sub_code(
            r"(?<![\w])const(?=\s+[A-Za-z_])",
            lambda m: "static const", body)
        text = text[:brace + 1] + body + text[close:]
    return text


# ---------------------------------------------------------------------------
# Types, statements, literals
# ---------------------------------------------------------------------------

#: Only the spellings that differ. `int`, `float`, `double`, `bool` and
#: `void` already mean in C what they mean in C#. `long` does *not*: C#
#: fixes it at 64 bits and C does not, so it is mapped rather than passed
#: through -- the one place in this table where a silent difference would
#: otherwise survive.
_TYPES = [
    ("sbyte", "signed char"),
    ("byte", "unsigned char"),
    ("ushort", "unsigned short"),
    ("uint", "unsigned int"),
    ("ulong", "unsigned long long"),
    ("long", "long long"),
    ("nint", "long"),
    ("nuint", "unsigned long"),
]


def _map_types(text):
    pat = r"(?<![\w.])(%s)(?![\w])" % "|".join(t for t, _ in _TYPES)
    repl = dict(_TYPES)
    return cpprust._sub_code(pat, lambda m: repl[m.group(1)], text)


def _lower_var(text):
    """`var` is `auto`, and `cpp_auto.resolve` already deduces one."""
    return cpprust._sub_code(r"(?<![\w.])var(?=\s+\w)",
                             lambda m: "auto", text)


def _lower_foreach(text):
    """`foreach (T x in xs)` -> `for (T x : xs)`.

    Length-preserving on both halves, so the statement's columns survive:
    `foreach` is seven characters against `for` plus four spaces, and ` in `
    is four against ` : ` plus one.
    """
    scan = _blank(text)
    out, pos = [], 0
    for m in re.finditer(r"(?<![\w])foreach\s*\(", scan):
        close = cpprust._match_paren(scan, m.end() - 1)
        if close is None:
            continue
        head = scan[m.end():close]
        kw = re.search(r"(?<![\w])in(?![\w])", head)
        if kw is None:
            continue
        out.append(text[pos:m.start()])
        out.append("for    " + text[m.start() + len("foreach"):m.end()])
        out.append(text[m.end():m.end() + kw.start()])
        out.append(":  ")
        pos = m.end() + kw.end()
    out.append(text[pos:])
    return "".join(out)


def _lower_literals(text):
    return cpprust._sub_code(r"(?<![\w.])null(?![\w])",
                             lambda m: "NULL", text)


def _lower_this(text):
    """`this.x` is C#'s member access; C++ wants `this->x`."""
    return cpprust._sub_code(r"(?<![\w.])this\.",
                             lambda m: "this->", text)


def _lower_collections(text):
    """`List<T>` / `Dictionary<K,V>` onto the prelude containers."""
    text = cpprust._sub_code(r"(?<![\w.])List\s*<",
                             lambda m: "std::vector<", text)
    text = cpprust._sub_code(r"(?<![\w.])Dictionary\s*<",
                             lambda m: "std::map<", text)
    return text


def _lower_generic_classes(text):
    """`class Box<T>` -> `template<typename T> class Box`.

    C# type-parameter lists are the weak form of C++ templates (no
    specialisation, no non-type parameters). Spelling them as
    `template<typename …>` is what lets cpprust's monomorphiser run.
    """
    scan = _blank(text)
    out, pos = [], 0
    for m in re.finditer(
            r"(?<![\w])(class|struct)\s+(\w+)\s*<([^>]+)>", scan):
        kinds = [p.strip() for p in m.group(3).split(",") if p.strip()]
        if not kinds:
            continue
        # Skip if any parameter looks like a value (`int N`), which C# does
        # not allow and which would be a non-type template parameter.
        if any(re.match(r"(int|long|bool|uint)\b", k) for k in kinds):
            continue
        params = ", ".join("typename %s" % k for k in kinds)
        out.append(text[pos:m.start()])
        out.append("template<%s> %s %s"
                   % (params, m.group(1), m.group(2)))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _lower_new(text, shared_names):
    """`T x = new T(args);` -> `T x(args);` (no temporary to copy).

    Shared types become `shared_ptr` locals constructed with `make_shared`,
    which is what makes `a = b` alias rather than copy.
    """
    def decl(m):
        typ, name, args = m.group(1), m.group(2), m.group(3).strip()
        # Generic ctor: `Box<int> b = new Box<int>(7)` -- group 1 is the
        # full `Box<int>` spelling.
        if typ.split("<")[0] in shared_names:
            return ("std::shared_ptr<%s> %s = std::make_shared<%s>(%s);"
                    % (typ, name, typ, args))
        if args:
            return "%s %s(%s);" % (typ, name, args)
        return "%s %s;" % (typ, name)

    # `Box<int>` needs the angle list in the type.
    text = cpprust._sub_code(
        r"(?<![\w.])([\w:]+(?:\s*<[^;<>]*>)?)\s+(\w+)\s*=\s*"
        r"new\s+\1\s*\(([^)]*)\)\s*;",
        decl, text)

    def expr(m):
        typ, args = m.group(1), m.group(2).strip()
        base = typ.split("<")[0]
        if base in shared_names:
            return "std::make_shared<%s>(%s)" % (typ, args)
        if args:
            return "%s(%s)" % (typ, args)
        return "%s()" % typ

    return cpprust._sub_code(
        r"(?<![\w.])new\s+([\w:]+(?:\s*<[^;<>]*>)?)\s*\(([^)]*)\)",
        expr, text)


def _lower_throw_catch(text):
    """`throw`/`catch` -> the checked `raise`/`except` model."""
    text = cpprust._sub_code(r"(?<![\w.])throw(?![\w])",
                             lambda m: "raise", text)
    # `catch` is 5 letters, `except` is 6 -- one column shifts on that line.
    text = cpprust._sub_code(r"(?<![\w.])catch(?=\s*\()",
                             lambda m: "except", text)
    return text


def _mark_except_functions(text):
    """A function body that `raise`s must be declared `except`."""
    scan = _blank(text)

    def maybe(m):
        head, body = m.group(1), m.group(2)
        if re.search(r"(?<![\w.])raise(?![\w])", body) and "except" not in head:
            return head + " except {" + body + "}"
        return m.group(0)

    # One nesting level of braces in the body is enough for the forms the
    # tests write; deeper nests still carry `raise` into a later cpprust
    # diagnostic, which is the escape hatch for a cs2cpp bug.
    return re.sub(
        r"((?:[\w:<>,\s\*\&]+)\s+\w+\s*\([^)]*\)\s*)"
        r"\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}",
        maybe, text)


def _lower_auto_properties(text):
    """`int Count { get; set; }` -> field + get_Count / set_Count.

    Expression stays on the same line so newline counts are preserved; the
    line grows, which is the same trade `template<…>` already makes.
    """
    prop = re.compile(
        r"(?<![\w.])([\w:<>,\s\*\&]+?)\s+(\w+)\s*\{\s*get\s*;\s*"
        r"(?:(?:public|private|protected|internal)\s+)?set\s*;\s*\}")
    names = []

    def repl(m):
        typ, name = m.group(1).strip(), m.group(2)
        names.append(name)
        field = "_" + name
        return ("%s %s; %s get_%s() { return this->%s; } "
                "void set_%s(%s v) { this->%s = v; }"
                % (typ, field, typ, name, field, name, typ, field))

    text = cpprust._sub_code(prop.pattern, repl, text)
    for name in sorted(set(names), key=len, reverse=True):
        text = cpprust._sub_code(
            r"\." + re.escape(name) + r"\s*=\s*([^;]+);",
            lambda m, n=name: ".set_%s(%s);" % (n, m.group(1)), text)
        text = cpprust._sub_code(
            r"\.(?!get_|set_)" + re.escape(name) + r"\b(?!\s*\()",
            lambda m, n=name: ".get_%s()" % n, text)
    return text


def _lower_delegates(text):
    """`delegate int D(int x);` -> `typedef int (*D)(int x);`."""
    return cpprust._sub_code(
        r"(?<![\w.])delegate\s+([\w:<>,\s\*\&]+?)\s+(\w+)\s*\(([^)]*)\)\s*;",
        lambda m: "typedef %s (*%s)(%s);"
                  % (m.group(1).strip(), m.group(2), m.group(3)),
        text)


def _lower_lambdas(text):
    """`x => x + 1` / `(int x) => x + 1` -> C++ lambdas cpprust already lowers."""
    scan = _blank(text)
    arrows = list(re.finditer(r"=>", scan))
    for m in reversed(arrows):
        j = m.start() - 1
        while j >= 0 and scan[j] in " \t":
            j -= 1
        if j < 0:
            continue
        if scan[j] == ")":
            depth, i = 0, j
            while i >= 0:
                if scan[i] == ")":
                    depth += 1
                elif scan[i] == "(":
                    depth -= 1
                    if depth == 0:
                        break
                i -= 1
            if depth != 0:
                continue
            args = text[i:j + 1]
            args_start = i
        elif scan[j].isalnum() or scan[j] == "_":
            i = j
            while i >= 0 and (scan[i].isalnum() or scan[i] == "_"):
                i -= 1
            args_start = i + 1
            args = "(" + text[args_start:j + 1] + ")"
        else:
            continue
        body_start = m.end()
        while body_start < len(scan) and scan[body_start] in " \t":
            body_start += 1
        depth, i, n = 0, body_start, len(scan)
        while i < n:
            c = scan[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif c in ",;" and depth == 0:
                break
            i += 1
        body = text[body_start:i].strip()
        inner = args[1:-1].strip()
        if inner and not re.search(r"\w+\s+\w+", inner):
            parts = [p.strip() for p in inner.split(",") if p.strip()]
            args = "(" + ", ".join("auto %s" % p for p in parts) + ")"
        text = (text[:args_start] + "[]%s { return %s; }" % (args, body)
                + text[i:])
        scan = _blank(text)
    return text


def _wrap_shared_locals(text, shared_names):
    if not shared_names:
        return text
    for name in shared_names:
        text = cpprust._sub_code(
            r"(?<!shared_ptr<)(?<![\w.])" + re.escape(name)
            + r"(?!\s*<)\s+(\w+)\s*=",
            lambda m, n=name: "std::shared_ptr<%s> %s =" % (n, m.group(1)),
            text)
        text = cpprust._sub_code(
            r"(?<!shared_ptr<)(?<![\w.])" + re.escape(name)
            + r"(?!\s*<)\s+(\w+)\s*;",
            lambda m, n=name: "std::shared_ptr<%s> %s;" % (n, m.group(1)),
            text)
    return text


def _shared_calls(text, shared_names):
    """Method calls on shared_ptr locals: `Type_method(var.get(), …)`."""
    if not shared_names:
        return text
    var_type = {}
    for name in shared_names:
        for m in re.finditer(
                r"std::shared_ptr<\s*" + re.escape(name) + r"\s*>\s+(\w+)",
                text):
            var_type[m.group(1)] = name
    for v, typ in sorted(var_type.items(), key=lambda kv: -len(kv[0])):
        def meth(m, typ=typ, v=v):
            method, args = m.group(1), m.group(2).strip()
            if args:
                return "%s_%s(%s.get(), %s)" % (typ, method, v, args)
            return "%s_%s(%s.get())" % (typ, method, v)

        text = re.sub(
            r"\b" + re.escape(v) + r"\.(\w+)\s*\(([^)]*)\)", meth, text)
        text = re.sub(
            r"\b" + re.escape(v) + r"\.(\w+)\b(?!\s*\()",
            v + r".get()->\1", text)
    return text


def _find_shared_names(text):
    """Class names marked `[Shared]` on the preceding attribute line."""
    scan = _blank(text)
    names = set()
    for m in re.finditer(
            r"\[\s*Shared\s*\][ \t]*\n[ \t]*(?:public\s+|private\s+)?"
            r"class\s+(\w+)", scan):
        names.add(m.group(1))
    for m in re.finditer(
            r"\[\s*Shared\s*\][ \t]+(?:public\s+|private\s+)?class\s+(\w+)",
            scan):
        names.add(m.group(1))
    return names


def _type_start(look, end):
    """Where does the type ending at `end` begin? -1 if there is none.

    Walks backwards, because that is the only direction in which `T[]` can
    be read: the brackets are the marker and the type is whatever precedes
    them. A regex was tried first and cannot do this -- a generic argument
    list nests (`List<List<int>>`), and the character class that stops a
    pattern running away also stops it matching the nested case, which is
    exactly where `byte[][][]` quietly lost its third dimension.

    Angle brackets are matched by depth, so the whole argument list comes
    along; then the qualified name in front of it, `::` separators
    included.
    """
    i = end
    while i > 0 and look[i - 1] in " \t":
        i -= 1
    if i > 0 and look[i - 1] == ">":
        depth, i = 0, i
        while i > 0:
            ch = look[i - 1]
            if ch == ">":
                depth += 1
            elif ch == "<":
                depth -= 1
                if depth == 0:
                    i -= 1
                    break
            elif ch in ";{}()":
                return -1
            i -= 1
        else:
            return -1
        while i > 0 and look[i - 1] in " \t":
            i -= 1
    if i == 0 or not (look[i - 1].isalnum() or look[i - 1] == "_"):
        return -1
    while i > 0 and (look[i - 1].isalnum() or look[i - 1] == "_"):
        i -= 1
    # A qualifier: `std::vector`, or a C# `System.Collections` spelling.
    while i > 1 and look[i - 2:i] == "::":
        j = i - 2
        while j > 0 and look[j - 1] in " \t":
            j -= 1
        if j == 0 or not (look[j - 1].isalnum() or look[j - 1] == "_"):
            break
        while j > 0 and (look[j - 1].isalnum() or look[j - 1] == "_"):
            j -= 1
        i = j
    return i


def _lower_arrays(text):
    """`T[]` -> `std::vector<T>`, and `.Length` -> `.size()`.

    Not `T name[]`, which is the declarator C would want. A C# array is a
    heap object that *carries its length*, and a C array does not carry
    anything -- which is why `foreach` over one cannot work: the range-`for`
    lowering needs either a written size or a container with `size()` and
    `operator[]`. `vector<T>` is the type in cpprust's prelude that has
    both, so it is what a C# array is, rather than what a C# array is
    approximated by.

    Spelled `std::vector` on purpose. The prelude is requested either by
    `#include <vector>` or by the qualified name, and emitting the include
    would push every line below it down by one -- which is the one thing
    this file does not do. The `std::` is stripped by the prelude itself a
    few passes later, so nothing downstream sees it.

    Applied repeatedly, innermost-first, so `int[][]` becomes
    `std::vector<std::vector<int>>`. An *empty* `[]` is the only thing
    matched, which is what makes this safe to run over whole statements:
    `a[i]` is an index and `new int[5]` is an allocation, and neither is
    ever written with nothing between the brackets.
    """
    for _ in range(16):
        look = _blank(text)
        m = re.search(r"\[\s*\]", look)
        if m is None:
            break
        start = _type_start(look, m.start())
        if start < 0:
            break
        text = (text[:start] + "std::vector<" + text[start:m.start()].strip()
                + ">" + text[m.end():])
    return cpprust._sub_code(r"\.Length(?![\w])", lambda m: ".size()", text)


# ---------------------------------------------------------------------------
# Plain data: blittable structs, `[StructLayout]`, `MemoryMarshal`
# ---------------------------------------------------------------------------
#
# C# calls a type *unmanaged* when it holds no references, all the way
# down: primitives, enums, and structs made only of those. Such a type is
# its bytes, which is what lets `MemoryMarshal` view one as a `byte` span
# without copying or marshalling. A C struct of the same fields is also its
# bytes, laid out by the same natural-alignment rule .NET uses for
# `LayoutKind.Sequential` -- so the lowering is a byte copy, and the bytes
# are the ones .NET would produce on the same machine.
#
# What does *not* carry over is anything with a pointer in it. A `string` or
# an array field is a reference in C# and a heap-owning struct here, and
# copying either as bytes would duplicate an owner. So "is this type
# unmanaged" is checked here, against the C# declarations, and a type that
# is not gets a C# diagnostic naming the field -- which is also what .NET
# does, at compile time for the `unmanaged` constraint.

#: Size of every C# primitive whose size is fixed by the language. `nint`
#: and `nuint` are unmanaged too but are pointer-sized, so they are in the
#: set below and not here: a layout that depends on the target cannot be
#: checked against a `Pack` written for one.
_PRIM_SIZE = {
    "sbyte": 1, "byte": 1, "bool": 1,
    "short": 2, "ushort": 2,
    "int": 4, "uint": 4, "float": 4,
    "long": 8, "ulong": 8, "double": 8,
}
_PRIM_UNMANAGED = set(_PRIM_SIZE) | set(["nint", "nuint"])

#: Member modifiers that say nothing about storage.
_FIELD_NOISE = ("public", "private", "protected", "internal", "readonly",
                "volatile", "new", "required", "unsafe")

_AUTO_PROP_BODY = re.compile(
    r"^\{\s*get\s*;\s*(?:(?:(?:public|private|protected|internal)\s+)?"
    r"(?:set|init)\s*;\s*)?\}$")

#: Words that can precede `x = ..` without declaring `x`.
_NOT_A_TYPE = frozenset(("return", "else", "in", "out", "ref", "case", "new",
                         "throw", "is", "as", "await", "yield", "goto"))


def _split_depth(text, sep=","):
    """Split at `sep` outside (), [], {} and <>; returns (offset, part)."""
    parts, depth, start = [], 0, 0
    for i, c in enumerate(text):
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append((start, text[start:i]))
            start = i + 1
    parts.append((start, text[start:]))
    return parts


def _instance_fields(body, name):
    """([(type, field)], [ctor arity]) for a blanked struct/class body.

    Every brace group at depth zero is a method, accessor or nested type
    body -- except an auto-property's `{ get; set; }`, which *is* storage
    (the compiler gives it a backing field) and is kept as one.
    """
    flat, i, n = [], 0, len(body)
    while i < n:
        c = body[i]
        if c == "{":
            j = cpprust._match_brace(body, i)
            if j is None:
                break
            flat.append(";" if _AUTO_PROP_BODY.match(body[i:j + 1])
                        else " @;")
            i = j + 1
            continue
        flat.append(c)
        i += 1
    fields, ctors = [], []
    for piece in "".join(flat).split(";"):
        piece = re.sub(r"\[[^\]]*\]", " ", piece)
        decl = piece.split("=", 1)[0] if "=>" not in piece else piece
        cm = re.search(r"(?<![\w.~])%s\s*\(([^()]*)\)" % re.escape(name),
                       decl)
        if cm:
            ctors.append(len([a for a in cm.group(1).split(",")
                              if a.strip()]))
        if "@" in piece or "=>" in piece or "(" in decl:
            continue
        words = decl.split()
        if not words or "static" in words or "const" in words:
            continue
        words = [w for w in words if w not in _FIELD_NOISE]
        text = " ".join(words)
        parts = [p.strip() for _, p in _split_depth(text)]
        first = parts[0].rsplit(None, 1)
        if len(first) != 2:
            continue
        typ = re.sub(r"\s+", "", first[0]) if "<" in first[0] else first[0]
        fields.append((typ, first[1]))
        for extra in parts[1:]:
            if extra:
                fields.append((typ, extra))
    return fields, ctors


def _type_table(text):
    """Every declared type by name: kind, base clause, fields, ctors."""
    scan = _blank(text)
    table = {}
    for kind, name, start, brace, close in _find_types(text):
        head = scan[start:brace]
        colon = head.find(":")
        info = {"kind": kind, "base": "", "fields": [], "ctors": [],
                "generic": "<" in (head[:colon] if colon >= 0 else head)}
        if colon >= 0:
            info["base"] = head[colon + 1:].strip()
        if kind in ("struct", "class"):
            info["fields"], info["ctors"] = _instance_fields(
                scan[brace + 1:close], name)
        table[name] = info
    return table


def _unmanaged_reason(typ, table, seen=()):
    """None if `typ` is unmanaged C# (plain bytes), else why not."""
    t = typ.strip()
    if t in _PRIM_UNMANAGED:
        return None
    if t.endswith("]"):
        return ("`%s` is an array: a reference to storage somewhere else, "
                "so the struct's bytes would hold an owner, not the data"
                % t)
    if t == "string":
        return ("`string` is a reference to character data stored "
                "somewhere else, so the struct's bytes would hold an owner, "
                "not the text")
    if "<" in t:
        return ("`%s` is generic, and its fields are not known until it is "
                "instantiated" % t)
    info = table.get(t)
    if info is None:
        return ("`%s` is not declared in this file, so its fields cannot be "
                "checked" % t)
    if info["kind"] == "enum":
        return None
    if info["kind"] != "struct":
        return ("`%s` is a %s, which is a reference type in C# -- its bytes "
                "are the reference, not the object" % (t, info["kind"]))
    if info["base"]:
        return ("struct `%s` implements an interface, and the lowered struct "
                "carries a vtable pointer the C# one does not have" % t)
    if info["generic"]:
        return "struct `%s` is generic" % t
    if t in seen:
        return "struct `%s` contains itself" % t
    for ftype, fname in info["fields"]:
        why = _unmanaged_reason(ftype, table, tuple(seen) + (t,))
        if why:
            return "field `%s.%s`: %s" % (t, fname, why)
    return None


def _is_plain_struct(name, table):
    """A struct that is nothing but its bytes, so zero bytes are `new T()`."""
    info = table.get(name)
    return (info is not None and info["kind"] == "struct"
            and not info["ctors"]
            and _unmanaged_reason(name, table) is None)


def _layout(typ, table, pack):
    """(size, align, [(field, offset)]) with member alignment capped at
    `pack` (0: natural), or (None, reason) when it has no fixed layout."""
    t = typ.strip()
    if t in _PRIM_SIZE:
        size = _PRIM_SIZE[t]
        return size, (min(size, pack) if pack else size), []
    info = table.get(t)
    if t in _PRIM_UNMANAGED:
        return None, ("`%s` is pointer-sized, so its offset depends on the "
                      "target" % t)
    if info is None or info["kind"] not in ("struct", "enum"):
        return None, _unmanaged_reason(t, table) or "`%s` is unknown" % t
    if info["kind"] == "enum":
        base = info["base"].strip()
        size = _PRIM_SIZE.get(base, 4)
        return size, (min(size, pack) if pack else size), []
    why = _unmanaged_reason(t, table)
    if why:
        return None, why
    off, align, offsets = 0, 1, []
    for ftype, fname in info["fields"]:
        size, fal, _sub = _layout(ftype, table, 0)
        if size is None:
            return None, fal
        if pack:
            fal = min(fal, pack)
        off = (off + fal - 1) // fal * fal
        offsets.append((fname, off))
        off += size
        align = max(align, fal)
    off = (off + align - 1) // align * align
    return off, align, offsets


_STRUCT_LAYOUT = re.compile(
    r"\[\s*(?:[\w.]+\.)?StructLayout(?:Attribute)?\s*\(([^\]]*)\)\s*\]")


def _lower_struct_layout(text, table, path):
    """`[StructLayout(..)]` is checked, then dropped.

    `Sequential` (and `Auto`, which lets the runtime choose and may as well
    choose this) is what a C struct already is, so there is nothing to emit.
    `Pack` is the hard part. gcc honours `#pragma pack`; shivyc parses it
    and lays the struct out naturally anyway -- so emitting the pragma would
    give one source two layouts depending on which compiler built it, and
    nothing would say so. Instead the packed layout is computed here and
    compared with the natural one. Equal (the usual case: fields already
    in size order) means `Pack` changes nothing and dropping it is exact.
    Different is refused, naming the field that would move.
    """
    scan = _blank(text)
    out, pos = [], 0
    for m in _STRUCT_LAYOUT.finditer(scan):
        target = _KIND.search(scan, m.end())
        if target is None or target.group(1) not in ("struct", "class"):
            raise CsError("%s`[StructLayout]` must be on a struct."
                          % _at(path, text, m.start()))
        name = target.group(2)
        parts = [p.strip() for p in cpprust._split_top(m.group(1))]
        kind = parts[0] if parts else ""
        if re.search(r"(?<![\w])Explicit$", kind):
            raise CsError(
                "%s`LayoutKind.Explicit` on `%s` places each field at a "
                "written `[FieldOffset]`, which can overlap fields -- a "
                "union. The lowering has no union yet. Use "
                "`LayoutKind.Sequential`." % (_at(path, text, m.start()), name))
        if not re.match(r"^(?:[\w.]+\.)?LayoutKind\s*\.\s*(Sequential|Auto)$",
                        kind):
            raise CsError(
                "%s`[StructLayout(%s)]` on `%s`: the layout kind must be "
                "written as `LayoutKind.Sequential` or `LayoutKind.Auto`."
                % (_at(path, text, m.start()), kind, name))
        named = {}
        for p in parts[1:]:
            kv = re.match(r"^(\w+)\s*=\s*(.+)$", p)
            if kv is None:
                raise CsError("%s`[StructLayout]` argument `%s` on `%s` is "
                              "not `Name = value`."
                              % (_at(path, text, m.start()), p, name))
            named[kv.group(1)] = kv.group(2).strip()
        for key in named:
            if key not in ("Pack", "Size", "CharSet"):
                raise CsError("%s`[StructLayout]` field `%s` on `%s` is not "
                              "in the subset; `Pack` and `Size` are."
                              % (_at(path, text, m.start()), key, name))
        pack = 0
        if "Pack" in named:
            if not re.match(r"^\d+$", named["Pack"]) or int(named["Pack"]) \
                    not in (0, 1, 2, 4, 8, 16, 32, 64, 128):
                raise CsError("%s`Pack = %s` on `%s` must be 0 or a power "
                              "of two up to 128."
                              % (_at(path, text, m.start()), named["Pack"],
                                 name))
            pack = int(named["Pack"])
        want = None
        if "Size" in named:
            if not re.match(r"^\d+$", named["Size"]):
                raise CsError("%s`Size = %s` on `%s` must be a number."
                              % (_at(path, text, m.start()), named["Size"],
                                 name))
            want = int(named["Size"])
        if pack or want:
            nat = _layout(name, table, 0)
            if nat[0] is None:
                raise CsError(
                    "%s`[StructLayout]` on `%s` fixes its layout, and that "
                    "layout cannot be checked here: %s."
                    % (_at(path, text, m.start()), name, nat[1]))
            if pack:
                packed = _layout(name, table, pack)
                if packed[0] != nat[0] or packed[2] != nat[2]:
                    moved = ["`%s` at %d instead of %d" % (f, po, no)
                             for (f, no), (_, po) in zip(nat[2], packed[2])
                             if po != no]
                    what = (", ".join(moved) if moved else
                            "size %d instead of %d" % (packed[0], nat[0]))
                    raise CsError(
                        "%s`Pack = %d` on `%s` would change its layout "
                        "(%s). Not every compiler Crust uses honours "
                        "`#pragma pack`, so the lowered struct cannot "
                        "promise the packed layout, and silently giving it "
                        "a different one would change its bytes. Order the "
                        "fields largest-first so there is no padding to "
                        "remove, or split a wide field into `byte`s."
                        % (_at(path, text, m.start()), pack, name, what))
            if want is not None and want != 0 and want != nat[0]:
                raise CsError(
                    "%s`Size = %d` on `%s` differs from its field size (%d). "
                    "Add an explicit padding field instead."
                    % (_at(path, text, m.start()), want, name, nat[0]))
        out.append(text[pos:m.start()])
        out.append(" " * (m.end() - m.start()))
        pos = m.end()
    out.append(text[pos:])
    text = "".join(out)
    left = re.search(r"(?<![\w])StructLayout(?![\w])", _blank(text))
    if left:
        raise CsError("%s`[StructLayout(..)]` is only read in its own "
                      "brackets, directly before its struct. Write it that "
                      "way." % _at(path, text, left.start()))
    return text


def _decl_before(scan, idx):
    """`(type_start, type, name)` of a `T x = ` ending at `idx`, else None."""
    head = scan[max(0, idx - 400):idx]
    m = re.search(r"(?<![\w.])([A-Za-z_]\w*(?:\s*<[^;{}()=]*>)?)\s+(\w+)"
                  r"\s*=\s*$", head)
    if m is None or m.group(1) in _NOT_A_TYPE:
        return None
    return idx - len(head) + m.start(1), m.group(1), m.group(2)


def _newlines(s):
    return s.count("\n")


def _zero(var, typ):
    return "; _cs_zero((unsigned char *)&%s, (int)sizeof(%s));" % (var, typ)


def _lower_object_initializers(text, table, path, need):
    """`T x = new T { A = 1, B = 2 };` -> `T x = new T(); x.A = 1; x.B = 2;`

    Only the declaration form, because that is the one with a name to
    assign through; in any other position the object is a temporary and the
    member assignments would need somewhere to live. The `new T()` left
    behind is the ordinary one, lowered by `_lower_new` as before.

    A plain struct -- no constructor, nothing but unmanaged fields -- is
    zeroed rather than constructed: `T x; _cs_zero(&x, ..)`. That is what C#
    `new T()` means for a struct, and it is load-bearing here. A field the
    initializer does not mention is 0 in C#; left uninitialised in C it is
    whatever was on the stack, and once the struct is serialised that
    garbage is in the bytes.

    Not `T x = {0};`, the spelling C would reach for: cpprust gives a struct
    with a struct member member-wise copy semantics, and refuses a brace
    list as the source of one. A byte loop after the declaration asks
    nothing of the type.
    """
    scan = _blank(text)
    m = re.search(r"(?<![\w.])new\s*(?:[\w.]+(?:\s*<[^;{}()]*>)?\s*)?"
                  r"\[[^\]]*\]\s*\{", scan)
    if m:
        raise CsError(
            "%san array initializer (`new T[] { .. }`) is not in the C# "
            "subset yet. Allocate with `new T[n]` and assign the elements."
            % _at(path, text, m.start()))
    pat = re.compile(r"(?<![\w.])new\s+([A-Za-z_][\w.]*(?:\s*<[^;{}()]*>)?)"
                     r"\s*(?:\(([^()]*)\))?\s*\{")
    edits = []
    for m in pat.finditer(scan):
        typ = m.group(1)
        open_b = m.end() - 1
        close_b = cpprust._match_brace(scan, open_b)
        if close_b is None:
            continue
        decl = _decl_before(scan, m.start())
        semi = re.match(r"\s*;", scan[close_b + 1:])
        if decl is None or semi is None:
            raise CsError(
                "%san object initializer (`new %s { .. }`) is only in the "
                "subset as a declaration, `%s x = new %s { .. };`. Declare "
                "a local, then use it."
                % (_at(path, text, m.start()), typ, typ, typ))
        tstart, dtype, var = decl
        if dtype != "var" and re.sub(r"\s+", "", dtype) != \
                re.sub(r"\s+", "", typ):
            raise CsError("%s`%s %s = new %s { .. }`: the declared type and "
                          "the constructed type must be the same."
                          % (_at(path, text, m.start()), dtype, var, typ))
        items = []
        inner = scan[open_b + 1:close_b]
        for off, part in _split_depth(inner):
            if not part.strip():
                continue
            im = re.match(r"^(\s*)(\w+)\s*=\s*(.*?)(\s*)$", part, re.DOTALL)
            if im is None or im.group(3).startswith("{"):
                raise CsError(
                    "%s`%s` in the initializer of `%s` is not a member "
                    "assignment `Name = value`. Collection and nested "
                    "initializers are not in the subset; assign after the "
                    "declaration." % (_at(path, text, open_b + 1 + off),
                                       part.strip(), var))
            base = open_b + 1 + off
            items.append((im.group(1), im.group(2),
                          text[base + im.start(3):base + im.end(3)],
                          im.group(4)))
        args = (m.group(2) or "").strip()
        plain = not args and _is_plain_struct(typ.strip(), table)
        start = m.start()
        if plain:
            # From the `=`: a declaration and a statement replace an
            # initialised declaration.
            start = scan.rfind("=", 0, m.start())
            first = _zero(var, typ.strip())
            need.add(("zero", ""))
        else:
            first = "new %s(%s);" % (typ, args)
        body = "".join("%s%s.%s = %s;%s" % (lead, var, name, val, trail)
                       for lead, name, val, trail in items)
        end = close_b + 1 + semi.end()
        repl = first + body
        repl += "\n" * (_newlines(text[start:end]) - _newlines(repl))
        edits.append((start, end, repl))
        if dtype == "var":
            edits.append((tstart, tstart + 3, typ))
    # `T x = new T();` for a plain struct: the same zeroing, no initializer.
    for m in re.finditer(r"(?<![\w.])new\s+(\w+)\s*\(\s*\)\s*;", scan):
        if not _is_plain_struct(m.group(1), table):
            continue
        decl = _decl_before(scan, m.start())
        if decl is None:
            continue
        need.add(("zero", ""))
        edits.append((scan.rfind("=", 0, m.start()), m.end(),
                      _zero(decl[2], m.group(1))))
        if decl[1] == "var":
            edits.append((decl[0], decl[0] + 3, m.group(1)))
    for start, end, repl in sorted(edits, reverse=True):
        text = text[:start] + repl + text[end:]
    return text


def _lower_new_arrays(text, table, path, need):
    """`new byte[n]` -> a zero-filled `vector<unsigned char>` of length n.

    C# `new T[n]` is `n` elements of `default(T)`, and for a primitive that
    is zero. The prelude's `vector(int)` only *reserves* -- length 0 -- so
    it is not this. A helper per element type does the fill; it is emitted
    once at the top of the file by `_emit_plain_helpers`.
    """
    scan = _blank(text)
    out, pos = [], 0
    for m in re.finditer(r"(?<![\w.])new\s+([\w.]+)\s*\[([^\[\]]+)\]", scan):
        elem = m.group(1)
        if scan[m.end():m.end() + 1] == "[" or \
                re.match(r"\s*\[", scan[m.end():]):
            raise CsError(
                "%s`new %s[..][..]` allocates a jagged array's outer level "
                "only, filled with nulls, and this subset has no null array. "
                "Build the rows in a loop." % (_at(path, text, m.start()),
                                              elem))
        info = table.get(elem)
        if info is not None and info["kind"] == "enum":
            # Zero is a value of every enum, as in C#. Spelled with the
            # underlying type: the helper is emitted at the top of the file,
            # above the enum's typedef, and `Kind` resolves to it anyway.
            base = info["base"].strip() or "int"
            need.add(("array", base))
            out.append(text[pos:m.start()])
            out.append("_cs_new_array_%s(%s)"
                       % (base, text[m.start(2):m.end(2)]))
            pos = m.end()
            continue
        if elem not in _PRIM_UNMANAGED:
            raise CsError(
                "%s`new %s[n]` is only in the subset for a primitive or enum "
                "element type. An array of `%s` starts as `n` default "
                "values, and for a type with an owner that is `n` objects "
                "nobody constructed. Allocate an array of a primitive -- "
                "indices, ids -- and keep the objects in fields."
                % (_at(path, text, m.start()), elem, elem))
        need.add(("array", elem))
        out.append(text[pos:m.start()])
        out.append("_cs_new_array_%s(%s)" % (elem, text[m.start(2):m.end(2)]))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _local_type(scan, var, before, path, text):
    """The declared C# type of local/field/parameter `var` above `before`."""
    found = None
    pat = (r"(?<![\w.])([A-Za-z_][\w.]*(?:\s*<[^;{}()=]*>)?)\s+%s"
           r"\s*(?=[=;,)])" % re.escape(var))
    for m in re.finditer(pat, scan[:before]):
        if m.group(1) not in _NOT_A_TYPE:
            found = m
    if found is None:
        raise CsError("%scannot find the declaration of `%s`. Declare it "
                      "with its type above this use."
                      % (_at(path, text, before), var))
    typ = found.group(1)
    if typ == "var":
        nm = re.match(r"\s*=\s*new\s+(\w+)", scan[found.end():])
        if nm is None:
            raise CsError("%s`%s` is declared `var` from something other "
                          "than `new T`, so its type is not known here. "
                          "Write the type." % (_at(path, text, before), var))
        typ = nm.group(1)
    return typ


def _check_blittable(typ, table, path, text, idx, what):
    why = _unmanaged_reason(typ, table)
    if typ in table and table[typ]["kind"] != "struct" and why is None:
        why = "`%s` is a %s" % (typ, table[typ]["kind"])
    if why:
        raise CsError(
            "%s%s needs `%s` to be an unmanaged struct -- nothing but "
            "primitives, enums and other such structs -- so that its bytes "
            "are all of it. It is not: %s."
            % (_at(path, text, idx), what, typ, why))


_MM = r"(?<![\w])(?:[\w]+\s*\.\s*)*?MemoryMarshal\s*\.\s*"


def _lower_memory_marshal(text, table, path, need):
    """The `MemoryMarshal` forms that serialise an unmanaged struct.

        MemoryMarshal.AsBytes(MemoryMarshal.CreateSpan(ref x, 1)).ToArray()
        MemoryMarshal.Read<T>(bytes)
        MemoryMarshal.Write(bytes, ref x)    // or `in x`, or `x`
        Marshal.SizeOf<T>()  /  Unsafe.SizeOf<T>()

    Each is a whole expression, recognised whole. `Span<byte>` itself is
    not in the subset -- a span is a pointer and a length into storage
    someone else owns, which is the thing single ownership exists to rule
    out -- so the span-returning pieces are only accepted where the span
    is consumed on the spot: `AsBytes(CreateSpan(..))` is only ever read
    by `.ToArray()`, which copies it into a `byte[]` the caller owns.

    Each lowers to a helper per struct, emitted after the struct by
    `_emit_plain_helpers`. A short buffer in `Read` or `Write` is
    `ArgumentOutOfRangeException` in .NET; the checked `except` model here
    would make every caller handle it, which C# callers do not do, so it
    aborts -- which is what the unhandled exception does.
    """
    forms = ("`MemoryMarshal.AsBytes(MemoryMarshal.CreateSpan(ref x, 1))"
             ".ToArray()`, `MemoryMarshal.Read<T>(bytes)` and "
             "`MemoryMarshal.Write(bytes, ref x)`")
    edits = []
    scan = _blank(text)
    for m in re.finditer(r"(?<![\w.])(?:Marshal|Unsafe)\s*\.\s*SizeOf\s*<\s*"
                         r"(\w+)\s*>\s*\(\s*\)", scan):
        typ = m.group(1)
        if typ not in _PRIM_UNMANAGED:
            _check_blittable(typ, table, path, text, m.start(),
                             "`SizeOf<%s>()`" % typ)
        edits.append((m.start(), m.end(), "((int)sizeof(%s))" % typ))
    skip_until = -1
    for m in re.finditer(_MM + r"(\w+)", scan):
        if m.start() < skip_until:
            continue
        member = m.group(1)
        at = m.start()
        rest = scan[m.end():]
        if member == "AsBytes":
            op = m.end() + len(rest) - len(rest.lstrip())
            close = cpprust._match_paren(scan, op) \
                if scan[op:op + 1] == "(" else None
            inner = scan[op + 1:close] if close is not None else ""
            cm = re.match(r"^\s*" + _MM + r"CreateSpan\s*\(\s*ref\s+(\w+)\s*,"
                          r"\s*(\w+)\s*\)\s*$", inner)
            tail = re.match(r"\s*\.\s*ToArray\s*\(\s*\)",
                            scan[close + 1:]) if close is not None else None
            if cm is None or tail is None:
                raise CsError(
                    "%s`MemoryMarshal.AsBytes` returns a `Span<byte>`, and "
                    "spans are not in the subset. The form that is: "
                    "`MemoryMarshal.AsBytes(MemoryMarshal.CreateSpan(ref x, "
                    "1)).ToArray()`, which copies the bytes into a `byte[]`."
                    % _at(path, text, at))
            if cm.group(2) != "1":
                raise CsError(
                    "%s`CreateSpan(ref %s, %s)`: only a count of `1` is in "
                    "the subset. A longer span runs past `%s` into whatever "
                    "memory follows it, which is only defined inside an "
                    "array." % (_at(path, text, at), cm.group(1),
                                cm.group(2), cm.group(1)))
            var = cm.group(1)
            typ = _local_type(scan, var, at, path, text)
            _check_blittable(typ, table, path, text, at,
                             "`MemoryMarshal.AsBytes` over `%s`" % var)
            end = close + 1 + tail.end()
            need.add(("bytes", typ))
            edits.append((at, end, "_cs_blit_bytes_%s(&%s)" % (typ, var)))
            skip_until = end
        elif member == "Read":
            rm = re.match(r"\s*<\s*(\w+)\s*>\s*\(", rest)
            if rm is None:
                raise CsError("%s`MemoryMarshal.Read` needs its type "
                              "written: `MemoryMarshal.Read<T>(bytes)`."
                              % _at(path, text, at))
            op = m.end() + rm.end() - 1
            close = cpprust._match_paren(scan, op)
            arg = text[op + 1:close].strip()
            typ = rm.group(1)
            _check_blittable(typ, table, path, text, at,
                             "`MemoryMarshal.Read<%s>`" % typ)
            ctors = table[typ]["ctors"]
            if ctors and 0 not in ctors:
                raise CsError(
                    "%s`MemoryMarshal.Read<%s>` makes a `%s` from bytes, and "
                    "the lowered struct can only be declared through a "
                    "parameterless constructor -- `%s` has constructors but "
                    "not that one. Add `public %s() { }`, or drop the "
                    "constructors." % (_at(path, text, at), typ, typ, typ,
                                       typ))
            if not re.match(r"^(?:this\s*\.\s*)?\w+$", arg):
                raise CsError(
                    "%s`MemoryMarshal.Read<%s>(%s)`: the source must be a "
                    "`byte[]` variable. Assign it to a local first."
                    % (_at(path, text, at), typ, arg))
            need.add(("read", typ))
            edits.append((at, close + 1,
                          "_cs_blit_read_%s(%s)" % (typ, arg)))
            skip_until = close + 1
        elif member == "Write":
            wm = re.match(r"\s*(?:<\s*(\w+)\s*>)?\s*\(", rest)
            if wm is None:
                raise CsError("%s`MemoryMarshal.Write` must be called."
                              % _at(path, text, at))
            op = m.end() + wm.end() - 1
            close = cpprust._match_paren(scan, op)
            args = [(o, p) for o, p in _split_depth(scan[op + 1:close])]
            src = args[1][1] if len(args) == 2 else ""
            vm = re.match(r"^\s*(?:(?:ref|in)\s+)?(\w+)\s*$", src)
            dest = text[op + 1 + args[0][0]:op + 1 + args[0][0]
                        + len(args[0][1])].strip()
            if vm is None or not re.match(r"^(?:this\s*\.\s*)?\w+$", dest):
                raise CsError(
                    "%s`MemoryMarshal.Write` is in the subset as "
                    "`MemoryMarshal.Write(bytes, ref x)`, both of them "
                    "variables." % _at(path, text, at))
            var = vm.group(1)
            typ = _local_type(scan, var, at, path, text)
            if wm.group(1) and wm.group(1) != typ:
                raise CsError("%s`MemoryMarshal.Write<%s>` is given `%s`, "
                              "which is a `%s`."
                              % (_at(path, text, at), wm.group(1), var, typ))
            _check_blittable(typ, table, path, text, at,
                             "`MemoryMarshal.Write` of `%s`" % var)
            need.add(("write", typ))
            edits.append((at, close + 1, "_cs_blit_write_%s(%s, &%s)"
                          % (typ, dest, var)))
            skip_until = close + 1
        else:
            raise CsError(
                "%s`MemoryMarshal.%s` is not in the C# subset. The "
                "`MemoryMarshal` forms that are: %s."
                % (_at(path, text, at), member, forms))
    for start, end, repl in sorted(edits, reverse=True):
        text = text[:start] + repl + text[end:]
    return text


def _helper_text(kind, typ):
    """C++-subset source of one generated helper, on one line."""
    # Emitted after every other pass, so in the final C spellings: the
    # primitive map has already run and will not see these.
    size = "(int)sizeof(%s)" % typ
    if kind == "array":
        ctype = dict(_TYPES).get(typ, typ)
        return ("static std::vector<%s> _cs_new_array_%s(int n) { "
                "std::vector<%s> v(n); int i = 0; if (n < 0) { abort(); } "
                "while (i < n) { v.push_back(0); i = i + 1; } return v; } "
                % (ctype, typ, ctype))
    if kind == "bytes":
        return ("static std::vector<unsigned char> _cs_blit_bytes_%s(%s *p) { "
                "std::vector<unsigned char> out(%s); unsigned char *s = (unsigned char *)p; int i = 0; "
                "while (i < %s) { out.push_back(s[i]); i = i + 1; } "
                "return out; } " % (typ, typ, size, size))
    if kind == "read":
        return ("static %s _cs_blit_read_%s(std::vector<unsigned char> &v) { "
                "%s r; unsigned char *d = (unsigned char *)&r; int i = 0; "
                "if (v.size() < %s) { abort(); } "
                "while (i < %s) { d[i] = v[i]; i = i + 1; } return r; } "
                % (typ, typ, typ, size, size))
    return ("static void _cs_blit_write_%s(std::vector<unsigned char> &v, %s *p) { "
            "unsigned char *s = (unsigned char *)p; int i = 0; "
            "if (v.size() < %s) { abort(); } "
            "while (i < %s) { v[i] = s[i]; i = i + 1; } } "
            % (typ, typ, size, size))


def _emit_plain_helpers(text, need):
    """Insert the helpers `need` names, each where its types are complete.

    `abort`, the zeroing loop and the array helpers use only primitives,
    so they go at the start of the first line of code. Struct helpers go directly after the struct's
    outermost enclosing type, where the struct is complete and the
    helpers are at file scope. Always on an existing line: the line count
    is the one invariant this file keeps.
    """
    if not need:
        return text
    edits = []
    # File scope, ahead of everything: declared inside a namespace, cpprust
    # would prefix `abort` with it, and the libc symbol would go missing.
    head = ["void abort(void); "]
    if ("zero", "") in need:
        head.append("static void _cs_zero(unsigned char *p, int n) { "
                    "int i = 0; while (i < n) { p[i] = 0; i = i + 1; } } ")
    head.extend(_helper_text("array", t)
                for t in sorted(t for k, t in need if k == "array"))
    first = re.search(r"\S", _blank(text))
    at = first.start() if first else len(text)
    edits.append((text.rfind("\n", 0, at) + 1, "".join(head)))
    types = _find_types(text)
    by_struct = {}
    for kind, typ in need:
        if kind in ("bytes", "read", "write"):
            by_struct.setdefault(typ, []).append(kind)
    for typ in sorted(by_struct):
        own = [t for t in types if t[1] == typ]
        if not own:
            continue
        _k, _n, start, _b, close = own[0]
        outer = [t for t in types if t[2] <= start and t[4] >= close]
        close = max(t[4] for t in outer)
        at = close + 1
        if text[at:at + 1] == ";":
            at += 1
        order = ("bytes", "read", "write")
        edits.append((at, " " + "".join(
            _helper_text(k, typ) for k in order if k in by_struct[typ])))
    for at, ins in sorted(edits, reverse=True):
        text = text[:at] + ins + text[at:]
    return text


def _check_top_level_statements(text, path):
    """Statements outside any type are C# 9's implicit `Main`: refused.

    C# requires them to come *before* every type declaration (CS8803), and C
    needs the opposite -- a function body cannot use a struct defined below
    it. Moving them would move their line numbers too, so for now they are
    refused, in C# terms, rather than passed to the C++ half as globals.
    """
    scan = _blank(text)
    chars = list(scan)
    for _kind, _name, start, _brace, close in _find_types(text):
        end = close + 1
        semi = re.match(r"\s*;", scan[end:])
        if semi:
            end += semi.end()
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "
    flat = "".join(chars)

    def gone(m):
        # Same length, newlines kept: the offset of what is left is the
        # offset in `text`, which is where the diagnostic has to point.
        return re.sub(r"[^\n]", " ", m.group(0))

    for pat in (r"(?<![\w])namespace\s+[\w.]+\s*\{", r"\[[^\]]*\]",
                r"(?<![\w])(?:global\s+)?using\s+[^;]*;",
                r"(?<![\w])delegate\s[^;]*;",
                r"(?<![\w])extern\s+alias\s[^;]*;"):
        flat = re.sub(pat, gone, flat)
    m = re.search(r"[;(=]", flat)
    if m:
        raise CsError(
            "%sa top-level statement is not in the C# subset yet. It is "
            "part of the implicit `Main` C# 9 builds from statements outside "
            "any type, which C# requires to come before every type "
            "declaration -- the reverse of what C needs, since a function "
            "cannot use a struct declared below it. Put the statements in a "
            "method: `public class Program { public int Run() { .. } }`."
            % _at(path, text, m.start()))


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
#
# C gives every enum member one namespace, file-wide; C# scopes a member to
# its type, so `Color.Red` and `Light.Red` are different names. Each member
# is therefore renamed after its type, `Kind.A` -> `Kind_A`, at the
# declaration and at every use.
#
# The type itself is not the C enum. It is a typedef of the C# underlying
# type, `int` unless written otherwise, and the members live in a separate
# `enum Kind_values`. C leaves an enum's size and signedness to the
# compiler -- gcc makes an enum with no negative member `unsigned`, and a
# `: byte` enum has no C spelling at all before C23 -- while C# fixes both.
# A `: byte` field that came out four bytes wide would move every field
# after it, which is exactly what a serialised struct cannot afford.

#: The underlying types C# allows, all integral.
_ENUM_BASES = ("sbyte", "byte", "short", "ushort", "int", "uint", "long",
               "ulong")

_FLAGS_ATTR = re.compile(r"\[\s*(?:System\s*\.\s*)?Flags(?:Attribute)?\s*\]")


def _namespace_names(scan):
    names = set()
    for m in re.finditer(r"(?<![\w])namespace\s+([\w.]+)", scan):
        names.update(m.group(1).split("."))
    return names


def _enum_decl(text, scan, name, start, brace, close, path):
    """(replacement, end) for one enum declaration, newlines kept."""
    head = scan[start:brace]
    colon = head.find(":")
    base = "int"
    if colon >= 0:
        base = head[colon + 1:].strip()
        base = re.sub(r"^System\s*\.\s*", "", base)
        full = {"SByte": "sbyte", "Byte": "byte", "Int16": "short",
                "UInt16": "ushort", "Int32": "int", "UInt32": "uint",
                "Int64": "long", "UInt64": "ulong"}
        base = full.get(base, base)
        if base not in _ENUM_BASES:
            raise CsError("%senum `%s` has underlying type `%s`; C# allows "
                          "only the integral types (%s)."
                          % (_at(path, text, start), name, base,
                             ", ".join(_ENUM_BASES)))
    body_scan = scan[brace + 1:close]
    parts = []
    for off, part in _split_depth(body_scan):
        pm = re.match(r"^(\s*)(\w+)(\s*)(?:=(.*))?$", part, re.DOTALL)
        if pm is None and part.strip():
            raise CsError("%s`%s` in enum `%s` is not a member."
                          % (_at(path, text, brace + 1 + off),
                             part.strip(), name))
        parts.append((brace + 1 + off, part, pm))
    names = set(pm.group(2) for _, _p, pm in parts if pm is not None)
    out = []
    for at, part, pm in parts:
        if pm is None:
            # Blank: the space after a trailing comma. Kept as written, so
            # the members keep their lines.
            out.append(text[at:at + len(part)])
            continue
        piece = pm.group(1) + "%s_%s" % (name, pm.group(2)) + pm.group(3)
        if pm.group(4) is not None:
            vstart = at + pm.start(4)
            value = text[vstart:at + pm.end(4)]
            for lit in re.finditer(r"(?<![\w.])(0[xX][0-9a-fA-F]+|\d+)",
                                   scan[vstart:at + pm.end(4)]):
                if int(lit.group(1), 0) > 0x7FFFFFFF:
                    raise CsError(
                        "%senum member `%s.%s` has a value past the `int` "
                        "range. A C enum constant is an `int`, and the "
                        "value would not survive the trip; keep it in "
                        "range, or use a `const long` instead."
                        % (_at(path, text, at), name, pm.group(2)))
            # A sibling named bare in an initializer (`B = A + 1`) is in
            # scope in C#, and is renamed like every other use of it.
            value = cpprust._sub_code(
                r"(?<![\w.])(%s)(?![\w])"
                % "|".join(re.escape(n) for n in sorted(names)),
                lambda m, _n=name: "%s_%s" % (_n, m.group(1)), value)
            piece += "=" + value
        out.append(piece)
    end = close + 1
    semi = re.match(r"\s*;", scan[end:])
    if semi and "\n" not in semi.group(0):
        end += semi.end()
    repl = ("enum %s_values {%s}; typedef %s %s;"
            % (name, ",".join(out), base, name))
    # Anything between the keyword and the brace -- the name, a base clause
    # -- may span lines; so may the body. Keep the newlines they held.
    repl += "\n" * (_newlines(text[start:end]) - _newlines(repl))
    return repl, end, names


def _lower_enums(text, path):
    """C# enums onto `enum T_values { T_A, .. }; typedef <base> T;`.

    A nested enum is hoisted: cpprust has no nested enum, and declaring one
    inside a struct emits `enum Kind;` as a member. It moves to just before
    the outermost type that contains it, collapsed onto that line, and its
    own lines are left as blank lines -- so every line keeps its number.
    """
    text = cpprust._sub_code(_FLAGS_ATTR.pattern,
                             lambda m: " " * len(m.group(0)), text)
    scan = _blank(text)
    types = _find_types(text)
    enums = [t for t in types if t[0] == "enum"]
    if not enums:
        return text
    seen = {}
    for _k, name, start, _b, _c in enums:
        if name in seen:
            raise CsError(
                "%stwo enums are named `%s` (the other at line %d). C puts "
                "enum members in one namespace per file, so both would "
                "declare `%s_..`; rename one."
                % (_at(path, text, start), name,
                   _line_of(text, seen[name]), name))
        seen[name] = start
    members = {}
    edits, hoist = [], {}
    for _k, name, start, brace, close in enums:
        repl, end, names = _enum_decl(text, scan, name, start, brace, close,
                                      path)
        members[name] = names
        outer = [t for t in types
                 if t[0] != "enum" and t[3] < start and t[4] > close]
        if outer:
            anchor = min(t[2] for t in outer)
            hoist.setdefault(anchor, []).append(repl.replace("\n", " ")
                                                .rstrip() + " ")
            edits.append((start, end, "\n" * _newlines(text[start:end])))
        else:
            edits.append((start, end, repl))
    for at, parts in hoist.items():
        edits.append((at, at, "".join(parts)))
    for start, end, repl in sorted(edits, key=lambda e: (e[0], e[1]),
                                   reverse=True):
        text = text[:start] + repl + text[end:]

    # Uses: `Kind.A`, `Outer.Kind.A`, `Net.Kind.A` -> `Kind_A`, and a
    # qualified type `Outer.Kind` -> `Kind`. A qualifier is only consumed
    # when it names a type or namespace in this file: `p.Kind.A` could be a
    # field that shares the enum's name, and is left alone.
    scan = _blank(text)
    quals = set(t[1] for t in _find_types(text)) | _namespace_names(scan)
    qual = r"(?:(?:%s)\s*\.\s*)*" % "|".join(
        re.escape(q) for q in sorted(quals, key=len, reverse=True))
    pat = re.compile(r"(?<![\w.])%s(%s)(?![\w])(?:\s*\.\s*(\w+))?"
                     % (qual, "|".join(re.escape(n) for n in members)))
    out, pos = [], 0
    for m in pat.finditer(scan):
        name, member = m.group(1), m.group(2)
        if member is None:
            repl = name
        elif member in members[name]:
            after = re.match(r"\s*\.\s*\w", scan[m.end():])
            if after:
                raise CsError(
                    "%s`%s.%s` is used as an object here, and an enum value "
                    "is a bare integer in the lowering: `ToString`, "
                    "`HasFlag` and the rest need the names in the binary. "
                    "Compare values, or switch on it."
                    % (_at(path, text, m.start()), name, member))
            repl = "%s_%s" % (name, member)
        else:
            raise CsError(
                "%s`%s.%s`: `%s` is not a member of enum `%s`. Enum methods "
                "(`Parse`, `GetValues`, `ToString` ..) are reflection over "
                "the members' names, which are not in the binary."
                % (_at(path, text, m.start()), name, member, member, name))
        out.append(text[pos:m.start()])
        out.append(repl)
        pos = m.end()
    out.append(text[pos:])
    text = "".join(out)

    # `Color Color;` is ordinary C#, and C would take it too -- but the C++
    # half resolves a typedef by substituting its name wherever it appears
    # as a word, so the field came out `int int;`. What is left of an enum's
    # name after the uses above is a type or a declared name, and a
    # declared one is refused here, naming the spelling that does work.
    scan = _blank(text)
    for m in re.finditer(r"(?<![\w.])([A-Za-z_][\w<>,\[\]]*)\s+(%s)"
                         r"\s*(?=[;=,)\[])"
                         % "|".join(re.escape(n) for n in members), scan):
        if m.group(1) in _NOT_A_TYPE or \
                re.search(r"(?<![\w])typedef\s+$", scan[:m.start()]):
            continue
        raise CsError(
            "%s`%s` is declared as a variable here, and is also the name of "
            "enum `%s`. The lowered enum type is a C typedef, and a "
            "declaration sharing its name collides with it. Rename it -- "
            "or, for a field, make it a property, `public %s %s "
            "{ get; set; }`, whose storage is renamed."
            % (_at(path, text, m.start(2)), m.group(2), m.group(2),
               m.group(1), m.group(2)))
    return text


def _drop_using_directives(text):
    """`using System;` goes; `using X = Y;` stays.

    The alias form is C++11's own spelling, and `cpp_auto.resolve_using_alias`
    already turns it into a typedef -- so it is passed through untouched
    rather than translated.
    """
    scan = _blank(text)
    out, pos = [], 0
    for m in re.finditer(r"^[ \t]*using\s+([\w.]+)\s*;[ \t]*$",
                         scan, re.MULTILINE):
        out.append(text[pos:m.start()])
        out.append(" " * (m.end() - m.start()))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


_ATTRIBUTE = re.compile(r"^[ \t]*\[\s*(\w+)[^\]\n]*\][ \t]*$", re.MULTILINE)


def _strip_attributes(text):
    """`[Serializable]` on its own line is dropped.

    Only the whole-line form, and only where the contents look like an
    attribute: `[` is also an index and an array declarator, and this pass
    has no business guessing between them. `[Shared]` -- the opt-in that
    CSHARP.md §1 reserves for reference semantics -- is recognised here when
    it is built, which is why the name is returned rather than discarded.
    """
    found = []
    scan = _blank(text)
    out, pos = [], 0
    for m in _ATTRIBUTE.finditer(scan):
        found.append(m.group(1))
        out.append(text[pos:m.start()])
        out.append(" " * (m.end() - m.start()))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), found


# ---------------------------------------------------------------------------

def translate(text, path="<cs>"):
    """Rewrite a C# subset source into the C++ subset. Raises CsError."""
    # Before a single character is rewritten, so every message names a C#
    # construct at a C# line.
    _check_refusals(text, path)

    shared = _find_shared_names(text)
    # Read from the source as written, before any pass renames a type.
    table = _type_table(text)
    need = set()
    # Before the generic attribute pass, which would drop a whole-line
    # `[StructLayout]` without reading its `Pack`.
    text = _lower_struct_layout(text, table, path)
    # Before anything reads a base clause: `enum E : byte` is not a base,
    # and `_qualify_bases` would make it `: public byte`.
    text = _lower_enums(text, path)
    text, attrs = _strip_attributes(text)
    # Attributes other than Shared are dropped; Shared only marks names.
    text = _drop_using_directives(text)
    text = _lower_delegates(text)
    # Interfaces before modifiers are dropped: an interface member is
    # implicitly public and may say so, and the pure-virtual rewrite reads
    # the declaration whole.
    text = _lower_interfaces(text, path)
    text = _lower_abstract(text, path)
    text = _strip_modifiers(text)
    # After the modifiers are gone, so `public const int N` is `const int N`
    # and the rename below sees a member and not a qualifier salad.
    text = _lower_constants(text)
    # While types still have their C# names and `var` is still `var`: each
    # of these reads a declaration to learn a type. Before the property
    # pass, so an initializer's `P = 1` becomes `x.P = 1` in time to be
    # turned into `x.set_P(1)` like any other.
    text = _lower_memory_marshal(text, table, path, need)
    text = _lower_object_initializers(text, table, path, need)
    text = _lower_new_arrays(text, table, path, need)
    text = _qualify_bases(text)
    text = _lower_generic_classes(text)
    text = _lower_auto_properties(text)
    # *Before* the primitive map, not after. Every C# primitive is one word
    # and several C spellings are two, so mapping first turned `byte[]` into
    # `unsigned char[]` -- where the element pattern matched `char` alone and
    # left `unsigned` stranded outside the `vector<..>` it belonged in. The
    # map below reaches inside the argument list and rewrites the element
    # there, which is the same work in the order that survives it.
    text = _lower_arrays(text)
    text = _lower_collections(text)
    text = _map_types(text)
    text = _lower_var(text)
    text = _lower_foreach(text)
    text = _lower_this(text)
    text = _lower_literals(text)
    text = _lower_throw_catch(text)
    text = _lower_lambdas(text)
    text = _lower_new(text, shared)
    text = _wrap_shared_locals(text, shared)
    text = _shared_calls(text, shared)
    text = _mark_except_functions(text)
    # Last: it inserts characters, and every pass above indexes the text it
    # was handed.
    text = _terminate(text)
    # After that: the helpers need each struct's final `};` to follow.
    text = _emit_plain_helpers(text, need)
    return text


def main():
    args = list(sys.argv[1:])
    out_path = None
    if "-o" in args:
        i = args.index("-o")
        if i + 1 >= len(args):
            sys.stderr.write("cs2cpp: -o needs a path\n")
            return 2
        out_path = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1 or out_path is None:
        sys.stderr.write("usage: cs2cpp.py <source.cs> -o <out.cpp>\n")
        return 2
    try:
        with open(args[0]) as f:
            text = f.read()
    except IOError as e:
        sys.stderr.write("cs2cpp: cannot read %s: %s\n" % (args[0], e))
        return 2
    try:
        result = translate(text, path=args[0])
    except CsError as e:
        try:
            with open(out_path, "w") as f:
                f.write(e.message)
        except IOError:
            pass
        sys.stderr.write("cs2cpp: %s\n" % e.message)
        return 1
    with open(out_path, "w") as f:
        f.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
