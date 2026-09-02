"""`tuple(xs)`, which is a list in this runtime.

`tuple` already maps to T_LIST in the type table, so `tuple(xs)` is the
same materialise as `list(xs)` -- but it had no lowering, and py2c emitted
a bare `tuple(...)` that C defaulted to returning int.

What is worth checking is not the constructor but the uses `cpprust` puts
it to: a tuple as a dict key, and `in` over a container of tuples. Those
are the places where "a tuple is just a list" could stop being true.
"""


def main():
    names = ["a", "b"]
    t = tuple(names)
    print("len " + str(len(t)))
    print("elems " + t[0] + t[1])
    print("empty " + str(len(tuple([]))))

    # `in` over a list of tuples, by value not identity.
    seen = [tuple(["int", "int"]), tuple(["char"])]
    print("in-yes " + str(tuple(["int", "int"]) in seen))
    print("in-no " + str(tuple(["long"]) in seen))

    # A tuple as a dict key, looked up by an equal tuple built separately.
    d = {}
    d[tuple(["k", "1"])] = "found"
    print("key " + str(d.get(tuple(["k", "1"]), "missing")))
    print("key-absent " + str(d.get(tuple(["k", "2"]), "missing")))


main()
