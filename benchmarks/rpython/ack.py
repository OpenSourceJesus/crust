"""Ackermann -- deep recursion stress (wired orphan; small args)."""
import sys


def ack(m: int, n: int) -> int:
    if m == 0:
        return n + 1
    if n == 0:
        return ack(m - 1, 1)
    return ack(m - 1, ack(m, n - 1))


def main() -> int:
    # argv: m n  (default 3 6 -- finishes quickly)
    m = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    return ack(m, n) % 256


if __name__ == "__main__":
    sys.exit(main())
