"""An rpython module included straight into a C translation unit.

`#include "histogram.py"` runs this through tools/py2c.py and splices the
generated C in where the directive stood, so the functions below are ordinary
C functions by the time the compiler proper sees them -- callable from C and
from Rust with no FFI, exactly like a `#include "vec2.rs"` module.

Everything here is typed and touches no lists, dicts or strings, so py2c
lowers it to plain C that needs nothing linked. A module that *does* use the
dynamic parts of the language still works; it just pulls shivyc_rt.c onto the
link line as well.
"""


def bucket_of(value: int, width: int) -> int:
    """Which fixed-width bucket a value falls in."""
    if width <= 0:
        return 0
    if value < 0:
        return 0
    return value // width


def bucket_count(lo: int, hi: int, width: int) -> int:
    """How many buckets span [lo, hi]."""
    if width <= 0 or hi < lo:
        return 0
    return bucket_of(hi - lo, width) + 1


def clamp(value: int, lo: int, hi: int) -> int:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def scale_to_width(count: int, total: int, width: int) -> int:
    """Bar length for `count` out of `total`, in a field `width` wide."""
    if total <= 0:
        return 0
    return clamp((count * width) // total, 0, width)
