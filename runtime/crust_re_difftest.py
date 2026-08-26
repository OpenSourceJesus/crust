#!/usr/bin/env python3
"""Differential fuzzer: crust_re vs CPython's `re`.

Builds crust_re_difftest.c, feeds it (pattern, text, mode) cases, and compares
every capture offset against the stdlib. Covers a fixed corpus (including every
static pattern used in the Crust tree) plus randomly generated patterns.

  python3 crust_re_difftest.py                # corpus + 20000 random cases
  python3 crust_re_difftest.py -n 200000      # more random cases
  python3 crust_re_difftest.py --seed 7       # reproduce a run

A divergence prints the pattern, the input, and both engines' capture tuples.
"""
import argparse
import os
import random
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Syntax crust_re deliberately does not implement. Patterns containing these
# are expected to fail compilation, so they are excluded from match testing.
UNSUPPORTED = ('(?#', '(?i', '(?m', '(?s', '(?x', '(?a', '(?P=')

# Variable-width lookbehind is rejected, exactly as CPython rejects it -- but
# CPython's own error arrives at compile time, so those cases land in the
# "both reject" bucket and need no exclusion here.

# Counted repetition applied to a group -- "(...)  {m,n}" or "(...){m,n}" -- is
# deliberately rejected by crust_re (see the note in parse_rep).
GROUP_BRACE = re.compile(r"\)\s*\{\s*\d")


def build(cc="cc"):
    exe = os.path.join(HERE, "crust_re_difftest.bin")
    cmd = [cc, "-std=c99", "-O1", "-Wall", "-Wextra", "-Wno-unused-parameter",
           "-o", exe,
           os.path.join(HERE, "crust_re_difftest.c"),
           os.path.join(HERE, "crust_re.c")]
    subprocess.run(cmd, check=True)
    return exe


class Engine:
    """Runs the whole case list through the driver in one batch.

    A per-case write/flush/readline round-trip dominated runtime (~100x), so
    cases are collected first and piped through a single invocation.
    """

    def __init__(self, exe):
        self.exe = exe

    def run_batch(self, cases):
        inp = "".join(
            "%s %s %s\n" % (mode, pat.encode("latin-1").hex(),
                            text.encode("latin-1").hex())
            for mode, pat, text in cases)
        p = subprocess.run([self.exe], input=inp, stdout=subprocess.PIPE,
                           text=True)
        out = p.stdout.splitlines()
        if len(out) != len(cases):
            raise SystemExit("driver returned %d lines for %d cases (crash?)"
                             % (len(out), len(cases)))
        return [self._parse(o) for o in out]

    @staticmethod
    def _parse(out):
        out = out.strip()
        if out.startswith("MATCH"):
            return tuple(int(x) for x in out.split()[1:])
        if out.startswith("ERR"):
            return ("ERR", out[4:])
        return (out,)


def cpython(mode, pat, text):
    try:
        c = re.compile(pat)
    except Exception:
        return ("ERR", "")
    m = c.match(text) if mode == "m" else c.search(text)
    if m is None:
        return ("NOMATCH",)
    out = []
    for g in range(c.groups + 1):
        s, e = m.span(g)
        out.extend([s, e])
    return tuple(out)


# ---- corpus ---------------------------------------------------------------

CORPUS_PATTERNS = [
    r"", r"a", r"abc", r"a.c", r".*", r".+", r"a*", r"a+", r"a?", r"a*?", r"a+?",
    r"a??", r"^abc", r"abc$", r"^abc$", r"^a*$", r"[abc]", r"[^abc]", r"[a-z]+",
    r"[a-zA-Z0-9_]", r"[^a-z]*", r"[]a]", r"[a-]", r"[-a]", r"\d", r"\D", r"\w",
    r"\W", r"\s", r"\S", r"\d+", r"\w*", r"\.", r"\\", r"\[", r"a|b", r"ab|cd",
    r"^a|b$", r"(a)", r"(a)(b)", r"(a|b)c", r"(ab)+", r"(ab)*", r"(ab)?",
    r"(a)(b)?", r"(?:ab)c", r"(?:a|b)+", r"a{2}", r"a{2,}", r"a{2,4}", r"a{0,2}",
    r"a{2,4}?", r"(ab){2}", r"(ab){1,3}", r"\bfoo", r"foo\b", r"\Bfoo", r"\bfoo\b",
    r"a\b", r"(a+)+b", r"(a*)*b", r"(a|a)*b", r"colou?r", r"[0-9]{1,3}\.[0-9]{1,3}",
    r"\s*(\w+)\s*=\s*(\w+)", r"^(\w+):(\d+)$", r"(a(b(c)))", r"((a)|(b))+",
    r"x(?:y|z)*w", r"a.*?b", r"a.*b", r"<(\w+)>.*?</\1>".replace(r"\1", "x"),
    r"[\d\s]+", r"[\w.-]+@[\w.-]+", r"a$", r"$", r"^", r"^$", r"\n", r"a\nb",
    # named groups
    r"(?P<w>\w+)", r"(?P<a>a)(?P<b>b)", r"(?P<n>\d+)-(?P<m>\d+)",
    r"(?P<x>a)?b", r"^(?P<k>\w+)=(?P<v>.*)$",
    # lookahead
    r"a(?=b)", r"a(?!b)", r"(?=\w)\w+", r"(?!\d)\w+", r"\w+(?=;)",
    r"foo(?=\s*\()", r"a(?!static\b)", r"(?=.*b)a+",
    # lookbehind (fixed width)
    r"(?<=x)y", r"(?<!x)y", r"(?<=\w)\d", r"(?<![\w.])\w+",
    r"(?<=ab)c", r"(?<=ab|cd)e", r"(?<![\w.>])name",
    # variable-width lookbehind: both engines must reject
    r"(?<=a*)b", r"(?<=ab|c)d",
]

CORPUS_TEXTS = [
    "", "a", "b", "ab", "abc", "abcd", "aaa", "aaaa", "aab", "abab", "ababab",
    "xyz", "a.c", "AbC", "123", "a1b2", "  spaced  ", "foo", "foobar", "barfoo",
    "foo bar", "x=1", "name: 42", "a\nb", "\n", "a\n", "color", "colour",
    "192.168", "u@h.com", "a|b", "a{2}", "[abc]", "aaaaaaaaaaaaaaaaaaaaaaaac",
    "-", "]", "_", "  ", "\t", "abcabcabc", "zzz",
]


def tree_patterns():
    """Every static `re.<fn>("...")` pattern in the Crust tree."""
    import ast
    import glob
    pats = set()
    root = os.path.dirname(HERE)
    files = (glob.glob(os.path.join(root, "tools", "**", "*.py"), recursive=True)
             + glob.glob(os.path.join(root, "shivyc", "**", "*.py"), recursive=True))
    for f in files:
        try:
            tree = ast.parse(open(f, encoding="utf-8", errors="replace").read())
        except Exception:
            continue
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "re" and n.args
                    and isinstance(n.args[0], ast.Constant)
                    and isinstance(n.args[0].value, str)):
                pats.add(n.args[0].value)
    return sorted(pats)


# ---- random pattern generation -------------------------------------------

ATOMS = ["a", "b", "c", ".", r"\d", r"\w", r"\s", r"\W", r"[ab]", r"[^ab]",
         "[a-c]", r"\.", "x", r"\b",
         # zero-width assertions: never quantified (see rand_pattern)
         "(?=a)", "(?!a)", "(?=[ab])", "(?![ab])", r"(?=\w)", r"(?!\d)",
         "(?<=a)", "(?<!a)", "(?<=[ab])", "(?<![ab])", r"(?<=\w)", "(?<=ab)"]
QUANTS = ["", "*", "+", "?", "*?", "+?", "??", "{2}", "{1,3}", "{0,2}", "{2,}"]


def rand_pattern(rng, depth=0):
    n = rng.randint(1, 4)
    out = []
    for _ in range(n):
        r = rng.random()
        if r < 0.18 and depth < 2:
            inner = rand_pattern(rng, depth + 1)
            r2 = rng.random()
            if r2 < 0.3:
                grp = "(?:" + inner + ")"
            elif r2 < 0.45:
                grp = "(?P<g%d>" % rng.randint(0, 9999) + inner + ")"
            else:
                grp = "(" + inner + ")"
            out.append(grp + rng.choice(QUANTS))
        elif r < 0.28 and depth < 2:
            out.append(rand_pattern(rng, depth + 1) + "|"
                       + rand_pattern(rng, depth + 1))
        else:
            atom = rng.choice(ATOMS)
            q = rng.choice(QUANTS)
            if atom == r"\b" or atom.startswith("(?"):
                q = ""      # quantifying a zero-width assertion is not useful
            out.append(atom + q)
    s = "".join(out)
    if depth == 0:
        if rng.random() < 0.12:
            s = "^" + s
        if rng.random() < 0.12:
            s = s + "$"
    return s


def rand_text(rng):
    alpha = rng.choice(["ab", "abc", "abcx", "a1 .", "ab\n"])
    return "".join(rng.choice(alpha) for _ in range(rng.randint(0, 12)))


# ---- driver ---------------------------------------------------------------

def check(pat, text, got, mode, stats, failures):
    exp = cpython(mode, pat, text)

    if got and got[0] == "LIMIT":
        stats["limit"] += 1
        return
    got_err = isinstance(got[0], str) and got[0] == "ERR"
    exp_err = isinstance(exp[0], str) and exp[0] == "ERR"

    if got_err:
        # Refusing a pattern is safe; silently mis-parsing is not. Only flag it
        # when the pattern is inside the documented subset.
        if not exp_err and not any(u in pat for u in UNSUPPORTED) \
                and not GROUP_BRACE.search(pat) \
                and not re.search(r"\\\\[1-9]", pat):
            stats["refused"] += 1
            stats.setdefault("refused_pats", set()).add((pat, got[1]))
        else:
            stats["ok_refused"] += 1
        return
    if exp_err:
        failures.append((mode, pat, text, "cpython-rejects", got))
        return

    stats["compared"] += 1
    if got != exp:
        failures.append((mode, pat, text, exp, got))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--num", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--cc", default="cc")
    ap.add_argument("--max-report", type=int, default=15)
    args = ap.parse_args()

    exe = build(args.cc)
    eng = Engine(exe)
    rng = random.Random(args.seed)
    stats = {"compared": 0, "limit": 0, "refused": 0, "ok_refused": 0}
    failures = []

    cases = []
    for pat in CORPUS_PATTERNS + tree_patterns():
        for text in CORPUS_TEXTS:
            for mode in ("m", "s"):
                cases.append((mode, pat, text))
    for _ in range(args.num):
        cases.append((rng.choice("ms"), rand_pattern(rng), rand_text(rng)))

    results = eng.run_batch(cases)
    for (mode, pat, text), got in zip(cases, results):
        check(pat, text, got, mode, stats, failures)

    print("compared        %d" % stats["compared"])
    print("step-limited    %d" % stats["limit"])
    print("refused (ok)    %d   [outside the documented subset]" % stats["ok_refused"])
    print("refused (BAD)   %d   [inside the subset -- should compile]" % stats["refused"])
    for p, why in sorted(stats.get("refused_pats", set()))[:args.max_report]:
        print("    %-40r %s" % (p, why))
    print("divergences     %d" % len(failures))
    for mode, pat, text, exp, got in failures[:args.max_report]:
        print("  mode=%s pat=%r text=%r\n    cpython=%r\n    crust_re=%r"
              % (mode, pat, text, exp, got))

    bad = len(failures) + stats["refused"]
    print("\n%s" % ("FAIL" if bad else "PASS"))
    return 1 if bad else 0


sys.exit(main())
