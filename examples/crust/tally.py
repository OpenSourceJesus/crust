"""An rpython module that uses the dynamic side of the language.

Unlike histogram.py, this one builds lists and strings, so py2c's output
leans on the transpiler runtime. `#include "tally.py"` handles that
automatically: shivyc_rt.h comes along with the generated C, and shivyc_rt.c
is compiled and added to the link line. Both the generated C and its object
file are cached under /tmp, so only the first build pays for either.
"""


def labels(n: int) -> str:
    """Comma-separated bucket labels, built through list + join."""
    parts: "list[str]" = []
    i = 0
    while i < n:
        parts.append("b%d" % i)
        i += 1
    return ",".join(parts)


def widest(names: str) -> int:
    """Length of the longest comma-separated field."""
    best = 0
    for part in names.split(","):
        if len(part) > best:
            best = len(part)
    return best
