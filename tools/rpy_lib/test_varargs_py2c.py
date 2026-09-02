"""`*args` with and without `**kwargs`, which lower to different signatures.

A function's C signature carries a kwargs slot only when it actually
declares `**kwargs`:

    def both(a, *rest, **kw)  ->  obj both(obj a, obj kw, int _n_rest, ...)
    def only_var(*texts)      ->  obj only_var(int _n_texts, ...)

The call site used to pass the dict either way, so `only_var("ab", "cde")`
came out as `only_var(dict_new(), 2, ...)` -- the vararg count one slot too
far right, and the C compiler rejecting the call. Both spellings are
exercised because fixing one is exactly what risks breaking the other.
"""


def both(a, *rest, **kw):
    n = a
    for r in rest:
        n = n + r
    return n


def only_var(*texts):
    n = 0
    for t in texts:
        n = n + len(t)
    return n


def main():
    print("both " + str(both(1, 2, 3)))
    print("both-empty " + str(both(7)))
    print("only_var " + str(only_var("ab", "cde")))
    print("only_var-empty " + str(only_var()))


main()
