import re

def main():
    print("esc: " + re.escape("a.b*c(d)"))
    print("esc2: " + re.escape("name_1"))
    fa = re.findall("\\d+", "a1 bb22 c333")
    print("findall: " + ",".join(fa))
    fg = re.findall("(\\w)=(\\d)", "a=1 b=2")
    print("findall1grp: " + ",".join(re.findall("(\\w)=\\d", "a=1 b=2")))
    print("sub: " + re.sub("\\d+", "N", "a1 bb22 c333"))
    print("sub2: " + re.sub("x", "-", "axbxc"))
    print("sub3: " + re.sub("(cat|dog)", "pet", "cat and dog"))
    print("subnone: " + re.sub("zzz", "Q", "abc"))
main()

# re.escape / re.findall / re.sub lowered by py2c onto crust_re. escape needs
# no engine (pure string work); findall and sub walk the subject with repeated
# matching at an offset rather than rescanning from zero, and advance one byte
# past an empty match so a pattern like "x*" terminates the way CPython's does.
