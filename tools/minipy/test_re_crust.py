import re
def main():
    m = re.search("(cat|dog)s", "hotdogs")
    if m:
        print("g0=" + m.group(0) + " g1=" + m.group(1))
    p = re.compile("(?P<k>\\w+)=(?P<v>\\d+)")
    m2 = p.match("port=8080")
    if m2:
        print("k=" + m2.group(1) + " v=" + m2.group(2))
    if re.search("(?<![\\w.])name", "a.name and name"):
        print("lookbehind ok")
    if re.match("^\\d+$", "12345"):
        print("digits ok")
main()

# Guest-visible regex, exercised three ways: CPython, the minipy reference VM,
# and the py2c-compiled native interpreter. The last of those reaches the
# crust_re C engine through rpy_lib/crustre.py -> the __re_search builtin ->
# a runtime-valued re.search in interp.py that py2c lowers to _cre_dyn. The
# patterns here need alternation, named groups and lookbehind, none of which
# the previous minire-backed `re` could express.
