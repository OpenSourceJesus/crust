"""`re.sub` with a function replacement.

The replacement is called once per match with the match object, and its
return value is spliced in. That means calling back into lowered code from
inside the substitution loop, with the same match list every other frontend
builds -- so `m.group(1)` inside the callback has to work like it does
anywhere else.

Previously this warned and substituted None, so `out = re.sub(pat, fn, s)`
assigned None to a string and the C would not compile. `tools/cpprust.py`
rewrites its `#line` directives and expands its template parameter packs
this way.

The awkward cases are here because they are where a substitution loop goes
wrong: a zero-width match (must step, and must not drop the byte stepped
over), a callback that returns the match unchanged, one that returns the
empty string, and a pattern that never matches.
"""
import re


def upper(m):
    return m.group(0).upper()


def brace(m):
    return "[" + m.group(1) + "]"


def blank(m):
    return ""


def keep(m):
    return m.group(0)


def main():
    print("upper " + re.sub(r"[a-z]+", upper, "ab CD ef"))
    print("group " + re.sub(r"<(\w+)>", brace, "x <tag> y <two>"))
    print("blank " + re.sub(r"\d", blank, "a1b2c3"))
    print("keep " + re.sub(r"\w", keep, "abc"))
    print("nomatch " + re.sub(r"zzz", upper, "abc"))
    print("empty-subject " + re.sub(r"a", upper, ""))
    # A zero-width pattern: every position matches, and the byte stepped
    # over must still reach the output.
    print("zerowidth " + re.sub(r"x*", blank, "abc"))
    # A string replacement through the same path still behaves as a string.
    repl = "-"
    print("strvar " + re.sub(r"\d", repl, "a1b2"))


main()
