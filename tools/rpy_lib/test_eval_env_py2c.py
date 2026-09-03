"""`eval(expr, {"__builtins__": {}}, {})` -- the sandboxing spelling.

Lowered only when both environments are literal dicts binding no name,
because then they provably cannot affect the result: with nothing bound,
an expression either references no name (and evaluates the same either
way) or fails either way. `tools/cpprust.py` folds integer template
arguments with it, behind a guard that the text is digits and operators
only.
"""


def main():
    print("add " + str(eval("1 + 2", {"__builtins__": {}}, {})))
    print("prec " + str(eval("2 + 3 * 4", {"__builtins__": {}}, {})))
    print("paren " + str(eval("(2 + 3) * 4", {"__builtins__": {}}, {})))
    print("mod " + str(eval("17 % 5", {"__builtins__": {}}, {})))
    print("neg " + str(eval("7 - 9", {"__builtins__": {}}, {})))
    print("nested " + str(eval("((8))", {"__builtins__": {}}, {})))


main()
