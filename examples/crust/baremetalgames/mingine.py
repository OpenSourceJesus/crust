"""mingine.py -- the level and rules layer of a tiny game engine, in rpython.

Included into a C translation unit with `#include "mingine.py"`, which runs
this through tools/py2c.py and splices the generated C in where the directive
stood. By the time the compiler proper sees it, every function below is an
ordinary C function, callable from C and from Rust with no FFI.

The split across the three languages is not arbitrary:

    mingine.rs   geometry and colour   -- types and methods, where ownership
                                          and `impl` blocks read best
    mingine.py   level rules and RNG   -- the fiddly integer logic that is
                                          quickest to write and read as Python
    mingine.c    the framebuffer       -- pointers and raw memory, where C is
                                          simply the right tool

Everything here is typed and touches no lists, dicts or strings, so py2c
lowers it to plain C that needs nothing linked.
"""


# ------------------------------------------------------------ determinism --
# A linear congruential generator: the same constants glibc uses. Games want
# randomness that is reproducible, and a bare-metal kernel has no getrandom(),
# so the state is threaded explicitly rather than hidden in a global.

def lcg_next(state: int) -> int:
    """The next state in the sequence."""
    return (state * 1103515245 + 12345) & 0x7FFFFFFF


def rand_below(state: int, n: int) -> int:
    """A value in [0, n) drawn from `state` (which the caller then advances)."""
    if n <= 0:
        return 0
    return (state >> 8) % n


def rand_range(state: int, lo: int, hi: int) -> int:
    """A value in [lo, hi]."""
    if hi <= lo:
        return lo
    return lo + rand_below(state, hi - lo + 1)


# ------------------------------------------------------------- tile logic --
# The level is a grid. These are the index computations that are easy to get
# subtly wrong in C and easy to read here.

def tile_index(col: int, row: int, cols: int) -> int:
    """Row-major index of a tile, or -1 when off the grid."""
    if col < 0:
        return -1
    if row < 0:
        return -1
    if col >= cols:
        return -1
    return row * cols + col


def tile_col(index: int, cols: int) -> int:
    if cols <= 0:
        return 0
    return index % cols


def tile_row(index: int, cols: int) -> int:
    if cols <= 0:
        return 0
    return index // cols


def tile_origin_x(col: int, tile_w: int, margin: int) -> int:
    """Pixel x of a tile's left edge."""
    return margin + col * tile_w


def tile_origin_y(row: int, tile_h: int, margin: int) -> int:
    return margin + row * tile_h


def tile_of_pixel(px: int, tile_w: int, margin: int) -> int:
    """Which column a pixel falls in; -1 when left of the grid."""
    if tile_w <= 0:
        return -1
    if px < margin:
        return -1
    return (px - margin) // tile_w


# --------------------------------------------------------------- patterns --
# Level shapes, as predicates over (col, row). A brick wall with a gap, a
# checkerboard, a pyramid: the sort of thing that is one readable expression
# here and a nest of conditions in C.

def is_brick(col: int, row: int, cols: int, rows: int) -> int:
    """A wall with a doorway in the middle of the bottom two courses."""
    if row >= rows - 2:
        gap: int = cols // 2
        if col == gap:
            return 0
        if col == gap - 1:
            return 0
    return 1


def is_checker(col: int, row: int) -> int:
    return (col + row) % 2


def is_pyramid(col: int, row: int, cols: int) -> int:
    """A centred triangle: wider as rows go down."""
    mid: int = cols // 2
    span: int = row
    if col < mid - span:
        return 0
    if col > mid + span:
        return 0
    return 1


# ---------------------------------------------------------------- motion --
# Animation curves. Integer-only so they behave identically on a machine with
# no FPU enabled, which is the state a kernel is in before it touches CR0.

def wave(t: int, amplitude: int, period: int) -> int:
    """A triangle wave in [-amplitude, amplitude]."""
    if period <= 0:
        return 0
    phase: int = t % period
    half: int = period // 2
    if phase < half:
        if half == 0:
            return 0
        return -amplitude + (2 * amplitude * phase) // half
    rest: int = phase - half
    if half == 0:
        return 0
    return amplitude - (2 * amplitude * rest) // half


def ease_in(t: int, span: int, distance: int) -> int:
    """Quadratic ease over `span` ticks, in integer arithmetic."""
    if span <= 0:
        return distance
    if t >= span:
        return distance
    return (distance * t * t) // (span * span)


# ----------------------------------------------------------------- rules --

def score_for(level: int, hits: int, ticks: int) -> int:
    """Points: more for a higher level, less the longer it took."""
    base: int = hits * 100 * (level + 1)
    penalty: int = ticks // 4
    if penalty > base:
        return 0
    return base - penalty


def level_speed(level: int) -> int:
    """How many pixels per tick things move at this level."""
    if level < 0:
        return 1
    return 1 + level // 2


def lives_left(start: int, misses: int) -> int:
    n: int = start - misses
    if n < 0:
        return 0
    return n
