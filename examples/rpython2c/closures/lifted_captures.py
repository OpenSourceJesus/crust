"""Nested functions: captures that travel by value, and nested unpacking.

Two shapes that `tools/cpprust.py` uses heavily and that lowered to C which
did not compile. Both produce *undeclared identifiers* rather than wrong
answers, so they fail loudly at the C compiler rather than quietly at
runtime -- which is why a fixture that merely runs is enough to pin them.

1. A sibling handed on as a *value*. `emit_one` never calls `tsub`; it
   passes it to `apply_twice`. That lowers to a `make_closure` carrying
   `tsub`'s captured values, emitted inside `emit_one` -- so `emit_one` has
   to capture them too, even though it never names them. The transitive
   walk used to follow calls only, and missed this.

2. A nested tuple target, `(a, b), (c, d) = ...`. The assignment path
   handled Name, Subscript and Attribute elements and asked `expr()` for
   anything else as an lvalue, which for a tuple emitted the element names
   without declaring them.

Exit code is 31: 21 from the closure chain, 10 from the unpack.
"""


def apply_twice(fn, x: "int") -> int:
    return fn(fn(x))


def pairs():
    return [(1, 2), (3, 4)]


def run(base: "int") -> int:
    scale = base + 2                  # captured by tsub, never named in emit_one
    offset = 3                        # ditto

    def tsub(v: "int") -> int:
        return v * scale + offset

    def emit_one(x: "int") -> int:
        # `tsub` is handed on, not called: the closure built for it here
        # needs `scale` and `offset` in scope.
        return apply_twice(tsub, x)

    return emit_one(1)                # tsub(1)=6, tsub(6)=21


def unpack() -> int:
    work = pairs()
    (a, b), (c, d) = work[-2], work[-1]
    return a + b + c + d              # 10


def main() -> int:
    return (run(1) + unpack()) % 256  # 21 + 10 = 31


if __name__ == "__main__":
    import sys
    sys.exit(main())
