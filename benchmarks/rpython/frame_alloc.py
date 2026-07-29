"""OS-shaped micro: bitmap frame allocator (CrustOS Frames shape)."""
import sys

FRAMES = 512


def alloc_rounds(rounds: int) -> int:
    words: "list[int]" = [0, 0, 0, 0, 0, 0, 0, 0]
    free = FRAMES
    used = 0

    def taken(f: int) -> int:
        return (words[f // 64] >> (f % 64)) & 1

    def take(f: int) -> None:
        nonlocal free
        if taken(f) == 0:
            words[f // 64] = words[f // 64] | (1 << (f % 64))
            free -= 1

    def release(f: int) -> None:
        nonlocal free
        if taken(f) == 1:
            words[f // 64] = words[f // 64] & ~(1 << (f % 64))
            free += 1

    def alloc() -> int:
        f = 0
        while f < FRAMES:
            if taken(f) == 0:
                take(f)
                return f
            f += 1
        return -1

    r = 0
    while r < rounds:
        got: "list[int]" = []
        i = 0
        while i < 17:
            f = alloc()
            if f >= 0:
                got.append(f)
                used += 1
            i += 1
        i = 0
        while i < len(got):
            release(got[i])
            used -= 1
            i += 1
        r += 1
    return (used + free + words[0]) % 256


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    return alloc_rounds(n)


if __name__ == "__main__":
    sys.exit(main())
