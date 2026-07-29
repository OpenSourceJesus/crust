"""OS-shaped micro: URL scheme routing table (CrustOS schemes.py shape)."""
import sys

_NAMES = ["sys", "memory", "file", "pipe", "irq", "debug", "gpu"]


def scheme_of(url: str) -> int:
    idx = url.find(":")
    if idx <= 0:
        return -1
    head = url[0:idx]
    i = 0
    while i < len(_NAMES):
        if _NAMES[i] == head:
            return i
        i += 1
    return -1


def route_all(urls: str) -> "list[int]":
    out: "list[int]" = []
    for url in urls.split(","):
        out.append(scheme_of(url))
    return out


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200000
    batch = "sys:/a,memory:/b,file:/c,bogus:/x,irq:/1,debug:/log,pipe:/p"
    acc = 0
    i = 0
    while i < n:
        kinds = route_all(batch)
        j = 0
        while j < len(kinds):
            acc += kinds[j] + 1
            j += 1
        i += 1
    return acc % 256


if __name__ == "__main__":
    sys.exit(main())
