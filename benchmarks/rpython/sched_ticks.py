"""OS-shaped micro: round-robin tick scheduler (CrustOS schedule shape)."""
import sys


def schedule(rounds: int, nctx: int) -> int:
    ticks: "list[int]" = []
    prio: "list[int]" = []
    state: "list[int]" = []
    i = 0
    while i < nctx:
        ticks.append(0)
        prio.append(i % 4)
        state.append(1 if i != nctx - 1 else 2)  # last blocked
        i += 1
    switches = 0
    r = 0
    while r < rounds:
        i = 0
        while i < nctx:
            if state[i] == 1:
                ticks[i] = ticks[i] + (4 - prio[i])
                switches += 1
            i += 1
        r += 1
    acc = switches
    i = 0
    while i < nctx:
        acc += ticks[i]
        i += 1
    return acc % 256


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500000
    return schedule(n, 8)


if __name__ == "__main__":
    sys.exit(main())
