"""`os.path.normpath`, and scalar helpers as operands.

normpath collapses '.', '..' and repeated slashes textually -- the same
lexical rule CPython uses, with no filesystem lookup, so a symlink is not
resolved. `tools/cpprust.py` normalises every candidate include path this
way, and the call had no lowering: it came out as a method call on an
undeclared `os_path`.

The '..' cases are the ones a hand-written version gets wrong: popping
past the root (nothing to pop), popping past a leading '..' in a relative
path (also nothing to pop, and the '..' must survive), and an expression
that normalises away to nothing at all (which is '.', not '').

The last two lines cover the other half of this commit: `i + s.find(..)`
puts a raw C long in an operand position, where it needs boxing.
"""
import os


def main():
    print("dots " + os.path.normpath("a/./b"))
    print("dotdot " + os.path.normpath("a/b/../c"))
    print("slashes " + os.path.normpath("a//b///c"))
    print("abs " + os.path.normpath("/a/../b"))
    print("past-root " + os.path.normpath("/../a"))
    print("rel-updot " + os.path.normpath("../../a"))
    print("mixed-updot " + os.path.normpath("a/../../b"))
    print("empties-to-dot " + os.path.normpath("a/.."))
    print("trailing " + os.path.normpath("a/b/"))
    print("join " + os.path.normpath(os.path.join("x/y", "../z")))

    look = "f(a, b) g"
    i = 2
    print("operand " + str(i + look[i:].find(")", 1)))
    print("operand2 " + str(look.find("(", 0) + 1))


main()
