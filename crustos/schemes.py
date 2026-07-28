"""CrustOS: the scheme layer, in rpython.

Redox's defining idea is that every resource is named by a URL and the kernel
routes an open to whichever *scheme* claims the prefix. Nearly all of that is
text handling and table lookup -- the shape of work the Crust subset is worst
at (no string type, no iterator protocol) and rpython is best at.

So it lives here, and `kernel.c` keeps what wants to be close to the machine.
py2c lowers a typed list to three words, which the Rust side walks directly as
a `PyList<i32>`, so nothing is copied at the boundary.
"""

SCHEME_NONE = -1

_NAMES = ["sys", "memory", "file", "pipe", "irq", "debug"]


def scheme_count() -> int:
    return len(_NAMES)


def scheme_name(kind: int) -> str:
    if kind < 0 or kind >= len(_NAMES):
        return "?"
    return _NAMES[kind]


def scheme_of(url: str) -> int:
    """Which scheme claims `url`, by its `name:` prefix."""
    idx = url.find(":")
    if idx <= 0:
        return SCHEME_NONE
    head = url[0:idx]
    i = 0
    while i < len(_NAMES):
        if _NAMES[i] == head:
            return i
        i += 1
    return SCHEME_NONE


def path_of(url: str) -> str:
    idx = url.find(":")
    if idx < 0:
        return url
    return url[idx + 1:]


def route_all(urls: str) -> "list[int]":
    """Route a comma-separated batch; one scheme id per URL.

    Batched so the Rust side gets a single list to walk rather than calling
    back per URL.
    """
    out: "list[int]" = []
    for url in urls.split(","):
        out.append(scheme_of(url))
    return out


def accepted(urls: str) -> "list[int]":
    """Indices of the URLs that name a registered scheme."""
    out: "list[int]" = []
    i = 0
    for url in urls.split(","):
        if scheme_of(url) != SCHEME_NONE:
            out.append(i)
        i += 1
    return out


def describe(kind: int, url: str) -> str:
    return "%s:%s" % (scheme_name(kind), path_of(url))
