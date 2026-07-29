"""N-body style force integration -- float loops (wired orphan)."""
import sys


def run(steps: int) -> int:
    x: "list[float]" = [0.0, 1.0, 2.0]
    y: "list[float]" = [0.0, 0.5, 1.5]
    vx: "list[float]" = [0.0, 0.0, 0.0]
    vy: "list[float]" = [0.0, 0.0, 0.0]
    m: "list[float]" = [1.0, 2.0, 3.0]
    dt = 0.001
    step = 0
    while step < steps:
        i = 0
        while i < 3:
            fx = 0.0
            fy = 0.0
            j = 0
            while j < 3:
                if i != j:
                    dx = x[j] - x[i]
                    dy = y[j] - y[i]
                    d2 = dx * dx + dy * dy + 0.01
                    inv = 1.0 / (d2 * d2)
                    fx = fx + m[j] * dx * inv
                    fy = fy + m[j] * dy * inv
                j = j + 1
            vx[i] = vx[i] + dt * fx
            vy[i] = vy[i] + dt * fy
            i = i + 1
        i = 0
        while i < 3:
            x[i] = x[i] + dt * vx[i]
            y[i] = y[i] + dt * vy[i]
            i = i + 1
        step = step + 1
    return int((x[0] + y[2] + x[1]) * 1000000.0) % 256


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    return run(n)


if __name__ == "__main__":
    sys.exit(main())
