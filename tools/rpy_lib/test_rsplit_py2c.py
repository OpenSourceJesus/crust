"""`s.rsplit(sep, maxsplit)` -- splitting from the right.

`tools/cpp_auto.py` uses `body.rsplit(None, 1)` to take the last word off a
declaration, and it had no lowering at all.

Splitting from the right is not splitting from the left reversed: with a
maxsplit, the *leftmost* piece is the one that keeps the leftover
separators, which is the opposite of `split`. That, whitespace runs for a
None separator, and the trailing-separator case are what this pins.
"""


def main():
    print("ws " + str("a b c".rsplit(None, 1)))
    print("ws-all " + str("a b c".rsplit(None, -1)))
    print("ws-runs " + str("a   b  c".rsplit(None, 1)))
    print("ws-trailing " + str("a b  ".rsplit(None, 1)))
    print("ws-zero " + str("a b c".rsplit(None, 0)))
    print("sep " + str("a,b,c".rsplit(",", 1)))
    print("sep-all " + str("a,b,c".rsplit(",", -1)))
    print("sep-zero " + str("a,b,c".rsplit(",", 0)))
    print("sep-absent " + str("abc".rsplit(",", 1)))
    print("sep-trailing " + str("a,b,".rsplit(",", 1)))
    print("sep-lead " + str(",a".rsplit(",", 1)))
    print("empty " + str("".rsplit(None, 1)))


main()
