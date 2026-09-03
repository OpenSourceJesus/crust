"""pos/endpos: a windowed search still sees the text around the window.

`pat.search(s, pos)` starts matching at `pos`, but the subject is still the
whole string -- so a lookbehind at `pos` reads what precedes it, and `endpos`
is where `$` anchors. The lowering used to hand the engine `text + pos`,
which loses that context and then reports *no match* rather than an error.

That is the shape worth a test: not a refusal, a different answer. Code that
windows a scan to avoid copying the subject -- `agg_re.search(look, lo, at)`
in `tools/cpprust.py`, where the whole point of the window is that the
lookbehind still sees the character before it -- would quietly stop matching,
and the C++ it translates would come out subtly wrong rather than failing.
"""
import re

LB = re.compile(r"(?<=;)x")
NLB = re.compile(r"(?<!;)x")
DOLLAR = re.compile(r"x$")
WORD = re.compile(r"\w+")
SEMI = re.compile(r";")


def show(label, m):
    if m:
        print(label + " match at " + str(m.start()) + ".." + str(m.end()))
    else:
        print(label + " no match")


def main():
    # The lookbehind reads back across pos: ';' sits at 0, the search at 1.
    show("lookbehind", LB.search(";x", 1))
    # A negative lookbehind sees the 'a' too, so it does NOT match ';x'.
    show("neg-lookbehind ax", NLB.search("ax", 1))
    show("neg-lookbehind ;x", NLB.search(";x", 1))
    # endpos is where `$` anchors, and bounds how far a match may run.
    show("endpos anchors", DOLLAR.search("xy", 0, 1))
    show("endpos excludes", DOLLAR.search("xy", 0, 2))
    # Offsets are absolute, not relative to the window.
    show("absolute offsets", WORD.search("  abc", 2))
    # A window that starts past every match finds nothing.
    show("past the end", SEMI.search("a;b", 2))
    # finditer over a window, with the same rules.
    for m in WORD.finditer("aa bb cc", 3):
        print("finditer " + m.group(0) + " at " + str(m.start()))
    for m in WORD.finditer("aa bb cc", 3, 5):
        print("windowed " + m.group(0) + " at " + str(m.start()))


if __name__ == "__main__":
    main()

# Guarded rather than a bare `main()`: this module has module-level globals,
# so py2c emits an initializer that executes the top-level statements -- and
# it *also* calls `main` as the entry point. An unguarded call therefore ran
# twice natively and once under CPython, and the diff blamed the regex.
