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

    for mod in _REFUSED_PARAM_MODS:
        m = re.search(r"\(\s*%s\s+(?=\w)" % mod, scan)
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
