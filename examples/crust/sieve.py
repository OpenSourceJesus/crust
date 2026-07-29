"""The list-building half, in rpython.

A sieve is exactly the shape rpython is short at and the Crust subset is not:
a growable list, built by appending. py2c lowers it to three words that Rust
walks directly.
"""


def sieve(limit: int) -> "list[int]":
    out: "list[int]" = []
    n = 2
    while n < limit:
        d = 2
        prime = True
        while d * d <= n:
            if n % d == 0:
                prime = False
                break
            d += 1
        if prime:
            out.append(n)
        n += 1
    return out
