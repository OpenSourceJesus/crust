#!/usr/bin/env python3
"""Several C# files as one translation unit (csrust A.cs B.cs ... -o out.c).

One file at a time, csrust knows only the classes that file declares. A call
into another file's class -- `m.Apply(ref v)`, `new Layer()`, a base class, a
field of another file's type -- has nothing to resolve against, and a `ref`
or `out` argument cannot even be told from an error: the callee's signature is
what says an `&` goes there. So the files are translated together.

The unit is the files' text, cut into top-level type declarations and put
back together with every base class above its subclasses (the one order the
C++ half cannot choose for itself: a derived struct lays out behind its base).
Everything else is left in the order it was written. Attributes and comments
travel with the type that follows them.

Diagnostics are reported against `unit.cs`; `Unit.remap` turns
`unit.cs:812:` back into `FMatrix.cs:250:`, so an error still names the C#
line the author wrote.
"""
import os
import re

import tools.cs2cpp as cs2cpp

NAME = "unit.cs"


class UnitError(cs2cpp.CsError):
    pass


class Chunk(object):
    def __init__(self, path, text, file_line, name=None, bases=()):
        self.path = path
        self.text = text
        self.file_line = file_line      # line of `text`'s first character
        self.name = name
        self.bases = list(bases)
        self.unit_line = 0


def _bases_of(scan, start, brace):
    """Names after the `:` of a type header (`class A<T> : B, I where ..`)."""
    head = scan[start:brace]
    m = re.search(r"(?<![\w.])(?:class|struct|interface)\s+\w+\s*(?:<[^{}]*?>)?\s*:\s*", head)
    if not m:
        return []
    rest = re.split(r"(?<![\w.])where(?![\w])", head[m.end():])[0]
    rest = re.sub(r"<[^<>]*>", "", rest)
    return [b.strip().split(".")[-1] for b in rest.split(",") if b.strip()]


def chunks_of(path, text):
    """Cut one file into chunks, one per top-level type."""
    # A byte-order mark is not text: Visual Studio writes one to every C# file, and
    # left in front of `using ..;` it kept that line from being dropped.
    text = text.lstrip("\ufeff")
    text = cs2cpp.blank_inactive_pp_regions(text, {"CRUST"})
    types = cs2cpp._find_types(text)
    top, end = [], -1
    for t in sorted(types, key=lambda t: t[2]):
        if t[2] > end:                  # not inside the previous top-level type
            top.append(t)
            end = t[4]
    scan = cs2cpp._blank(text)
    out, prev = [], 0
    for kind, name, start, brace, close in top:
        out.append(Chunk(path, text[prev:close + 1], text.count("\n", 0, prev) + 1,
                         name, _bases_of(scan, start, brace)))
        prev = close + 1
    tail = text[prev:]
    if tail.strip():
        out.append(Chunk(path, tail, text.count("\n", 0, prev) + 1))
    return out


def order(chunks):
    """Stable: each type after the bases it names that this unit defines."""
    where = {}
    for c in chunks:
        if c.name:
            where.setdefault(c.name, c)
    done, out, trail = set(), [], []

    def visit(c):
        if id(c) in done:
            return
        if c in trail:
            raise UnitError("%s: a cycle in base classes: %s" % (
                NAME, " -> ".join(x.name for x in trail[trail.index(c):] + [c])))
        trail.append(c)
        for b in c.bases:
            dep = where.get(b)
            if dep is not None and dep is not c:
                visit(dep)
        trail.pop()
        done.add(id(c))
        out.append(c)

    for c in chunks:
        visit(c)
    return out


class Unit(object):
    def __init__(self, sources):
        """sources: [(path, text)]."""
        chunks = []
        seen = {}
        for path, text in sources:
            for c in chunks_of(path, text):
                if c.name and c.name in seen:
                    raise UnitError("%s: `%s` is declared twice: %s and %s" % (
                        NAME, c.name, os.path.basename(seen[c.name]),
                        os.path.basename(path)))
                if c.name:
                    seen[c.name] = path
                chunks.append(c)
        self.chunks = order(chunks)
        line = 1
        parts = []
        for c in self.chunks:
            c.unit_line = line
            parts.append(c.text)
            line += c.text.count("\n") + 1          # + the joining newline
        self.text = "\n".join(parts)

    def locate(self, line):
        """(path, line in that file) for a line of the unit."""
        best = None
        for c in self.chunks:
            if c.unit_line <= line:
                best = c
            else:
                break
        if best is None:
            return NAME, line
        return best.path, best.file_line + (line - best.unit_line)

    def remap(self, message):
        """`unit.cs:812: ..` -> `FMatrix.cs:250: ..`."""
        def one(m):
            path, ln = self.locate(int(m.group(1)))
            return "%s:%d:" % (os.path.basename(path), ln)
        return re.sub(r"(?<![\w.])%s:(\d+):" % re.escape(NAME), one, message)
