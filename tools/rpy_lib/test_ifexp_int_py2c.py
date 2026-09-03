"""A conditional with two integer branches stays a scalar.

`j = n if j < 0 else j` where one branch is an int and the other a long
used to box both into an obj -- and the boxed conditional then would not
assign back to the `long j` it came from. C's own conversions do this
anyway; boxing was throwing away a scalar for no reason.

The three positions are all here because the emitter, the type oracle and
the boxing helper each had to be taught the same rule, and they disagreed
one at a time: assignment (the scalar must stay assignable), return (an
obj-returning function must still box it), and argument (likewise).
"""


def find_or_end(text, sub):
    n = len(text)
    j = text.find(sub, 0)
    j = n if j < 0 else j        # assignment: int branch, long branch
    return j


def boxed(text, sub):
    # An obj-returning function: the unified scalar has to be boxed again.
    j = text.find(sub, 0)
    return j if j >= 0 else len(text)


def main():
    print("hit " + str(find_or_end("abcdef", "cd")))
    print("miss " + str(find_or_end("abcdef", "zz")))
    print("boxed-hit " + str(boxed("abcdef", "cd")))
    print("boxed-miss " + str(boxed("abcdef", "zz")))
    # Argument position.
    print("arg " + str(max(0, 3 if len("abc") > 2 else -1)))


main()
