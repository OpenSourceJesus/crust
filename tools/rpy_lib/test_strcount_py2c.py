"""`s.count(sub, start[, end])` over a window.

`tools/cpprust.py` turns an offset into a line number by counting newlines
up to it, and that spelling had no lowering at all -- py2c emitted a bare
`count(...)` which C defaulted to returning int, so the call did not
compile. Only the 2- and 3-argument forms are lowered as a string count:
with one argument the receiver could equally be a list.

The edges are what a hand-written counter gets wrong, so they are all here:
an empty needle (Python counts the gaps), a window that starts past the
end, a negative start, an end beyond the string, and an overlapping needle
(non-overlapping counting, so "aaa".count("aa") is 1).
"""


def main():
    text = "a\nbb\nccc\n"
    print("newlines " + str(text.count("\n", 0, len(text))))
    print("prefix " + str(text.count("\n", 0, 4)))
    print("suffix " + str(text.count("\n", 4)))
    print("empty-window " + str(text.count("\n", 5, 5)))
    print("start-past-end " + str(text.count("\n", 99)))
    print("end-past-end " + str(text.count("\n", 0, 999)))
    print("negative-start " + str(text.count("\n", -4)))
    print("empty-needle " + str("abc".count("", 0, 3)))
    print("overlapping " + str("aaaa".count("aa", 0, 4)))
    print("absent " + str("abc".count("z", 0, 3)))


main()
