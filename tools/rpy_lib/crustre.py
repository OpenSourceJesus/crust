# crustre: the `re` module minipy hands to guest scripts, backed by the crust_re
# engine rather than by a second Python implementation.
#
# The seam is `re.search(pattern, text)` with a *runtime* pattern. py2c lowers
# that to crust_re's dynamic bridge, and minipy's interpreter is itself compiled
# by py2c -- so when a guest script calls re.search, the pattern travels through
# this module and lands in the same C engine that backs the C and C++ frontends.
# Under CPython (and the minipy reference VM) the identical calls resolve to the
# stdlib, which is what makes the 3-way output comparison meaningful.
#
# This replaces minire for guests. minire remains as the pure-Python engine for
# contexts with no compiled core to call into; the two are not kept in sync,
# because that is exactly the duplicated rule set this module exists to avoid.
#
# Written in the minipy subset: classes, while loops, no comprehensions, no
# imports beyond `re` itself.

# __re_search / __re_match are minipy builtins (see minipy/compiler.py
# BUILTINS and interp.py do_builtin). They take (pattern, text) and return the
# flat capture list [whole, g1, ...] or None. There is deliberately no `import
# re` here: minipy maps `re` to this very module, so importing it would recurse.


class error(Exception):
    pass


# A match is a list of captured strings [whole, g1, g2, ...] -- the same shape
# the engine returns, and truthy exactly when it matched.
class Match:
    def __init__(self, groups):
        self._g = groups

    def _ng(self):
        # Layout is [g0..gn, s0,e0, ..., sn,en], so the group count falls out
        # of the length: len == 3*(ng+1).
        return len(self._g) // 3

    def group(self, n=0):
        if n < 0 or n >= self._ng():
            raise error("no such group: " + str(n))
        return self._g[n]

    def start(self, n=0):
        return self._g[self._ng() + 2 * n]

    def end(self, n=0):
        return self._g[self._ng() + 2 * n + 1]

    def span(self, n=0):
        return (self.start(n), self.end(n))

    def groups(self):
        out = []
        i = 1
        while i < self._ng():
            out.append(self._g[i])
            i = i + 1
        return out

    def lastindex(self):
        return self._ng() - 1


class Pattern:
    def __init__(self, pat):
        self.pattern = pat

    def match(self, s):
        return _wrap(__re_match(self.pattern, s))

    def search(self, s):
        return _wrap(__re_search(self.pattern, s))

    def findall(self, s):
        return findall(self.pattern, s)

    def sub(self, rep, s):
        return sub(self.pattern, rep, s)


def _wrap(m):
    # The builtin hands back the flat capture list, or None for no match.
    if m is None:
        return None
    return Match(m)


def escape(s):
    # Backslash everything that is not [A-Za-z0-9_], as CPython does.
    out = ""
    i = 0
    while i < len(s):
        c = s[i]
        plain = (("a" <= c and c <= "z") or ("A" <= c and c <= "Z")
                 or ("0" <= c and c <= "9") or c == "_")
        if not plain:
            out = out + "\\"
        out = out + c
        i = i + 1
    return out


def _walk(pat, s, want_sub, rep):
    """Shared engine for findall and sub.

    Both need the same scan -- repeated search over the tail of the subject,
    stepping one character past an empty match so a pattern like "x*" cannot
    spin. Keeping them in one function means the two cannot disagree about
    where a match ended.
    """
    found = []
    out = ""
    rest = s
    while True:
        m = __re_search(pat, rest)
        if m is None:
            break
        whole = m[0]
        idx = rest.find(whole)
        if idx < 0:
            break
        if want_sub:
            out = out + rest[0:idx] + rep
        else:
            # One group: findall yields that group, else the whole match.
            if len(m) // 3 == 2:
                found.append(m[1])
            else:
                found.append(whole)
        if len(whole) == 0:
            if idx >= len(rest):
                break
            if want_sub:
                out = out + rest[idx:idx + 1]
            rest = rest[idx + 1:]
        else:
            rest = rest[idx + len(whole):]
    if want_sub:
        return out + rest
    return found


def findall(pat, s):
    return _walk(pat, s, 0, "")


def sub(pat, rep, s):
    return _walk(pat, s, 1, rep)


def compile(pat):
    return Pattern(pat)


def match(pat, s):
    return _wrap(__re_match(pat, s))


def search(pat, s):
    return _wrap(__re_search(pat, s))
