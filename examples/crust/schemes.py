"""small_os: the string- and list-shaped half, in rpython.

Redox's defining idea is that every resource is named by a URL --
`file:/etc/passwd`, `disk:/0`, `tcp:/80` -- and the kernel routes an open call
to whichever *scheme* claims that prefix. Almost all of that is text handling
and table lookup, which is exactly the shape of work the Crust subset is worst
at and rpython is best at: no iterator protocol needed, no hand-rolled string
type, no manual buffer arithmetic.

So the routing table, the URL parser and the listings live here, and
`small_os.c` keeps what actually wants to be close to the machine: the context
table, the frame allocator's bitmap, the scheduler loop and the syscall
dispatch.

Everything here returns either an `int` or a typed list, both of which py2c
lowers to plain C -- a typed list becomes three words that Rust walks directly
as a `PyList<T>`, with no copy and no conversion at the boundary.
"""

# Scheme identifiers. These are small integers so the Rust side can switch on
# them; the names live here because naming is text work.
SCHEME_FILE = 0
SCHEME_DISK = 1
SCHEME_TCP = 2
SCHEME_PIPE = 3
SCHEME_NONE = -1

_NAMES = ["file", "disk", "tcp", "pipe"]


def scheme_count() -> int:
    return len(_NAMES)


def scheme_name(kind: int) -> str:
    """The registered name of a scheme, or `?` if there is no such scheme."""
    if kind < 0 or kind >= len(_NAMES):
        return "?"
    return _NAMES[kind]


def scheme_of(url: str) -> int:
    """Which scheme claims `url`, by its `name:` prefix.

    Returns SCHEME_NONE for a URL with no prefix or an unregistered one --
    the kernel turns that into ENOENT rather than guessing.
    """
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
    """The part after `name:`, or the whole string if there is no prefix."""
    idx = url.find(":")
    if idx < 0:
        return url
    return url[idx + 1:]


def depth_of(url: str) -> int:
    """How many non-empty path segments a URL has: `file:/a/b` -> 2."""
    n = 0
    for part in path_of(url).split("/"):
        if len(part) > 0:
            n += 1
    return n


def is_absolute(url: str) -> bool:
    path = path_of(url)
    return len(path) > 0 and path[0] == "/"


def route_all(urls: str) -> "list[int]":
    """Route a comma-separated batch of URLs; one scheme id per URL.

    Batching is what makes this worth crossing the boundary for: the Rust side
    gets a single list it can walk, rather than calling back per URL.
    """
    out: "list[int]" = []
    for url in urls.split(","):
        out.append(scheme_of(url))
    return out


def depths_all(urls: str) -> "list[int]":
    out: "list[int]" = []
    for url in urls.split(","):
        out.append(depth_of(url))
    return out


def accepted_indices(urls: str) -> "list[int]":
    """Positions of the URLs that name a registered scheme."""
    out: "list[int]" = []
    i = 0
    for url in urls.split(","):
        if scheme_of(url) != SCHEME_NONE:
            out.append(i)
        i += 1
    return out


def describe(kind: int, url: str) -> str:
    """A one-line listing entry, the way a `ls` on a scheme would print it."""
    return "%s -> %s (depth %d)" % (scheme_name(kind), path_of(url),
                                    depth_of(url))
