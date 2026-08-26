import re

def main():
    m = re.search("(cat|dog)s", "hotdogs here")
    if m:
        print("alt group: " + m.group(1))
    else:
        print("alt: no match")
    m2 = re.match("(?:ab)+c", "ababc")
    if m2:
        print("noncap whole: " + m2.group(0))
    p = re.compile("(\\w+)=(\\d+)")
    m3 = p.search("port=8080")
    if m3:
        print("kv: " + m3.group(1) + " -> " + m3.group(2))
    m4 = re.search("^\\d+$", "12345")
    if m4:
        print("tier1 digits: " + m4.group(0))

main()

# Run under CPython, the minipy reference VM and the py2c-compiled binary; all
# three must print the same thing. The patterns above are chosen to straddle
# both tiers: "(cat|dog)s" and "(?:ab)+c" need the crust_re VM, "^\d+$" is
# handled by the translation-time specializer, and both appear in one program
# so the dispatch between them is exercised.
