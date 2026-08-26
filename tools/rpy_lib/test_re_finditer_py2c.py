import re

def main():
    for m in re.finditer("(\\w+)=(\\d+)", "a=1 bb=22 ccc=333"):
        print("m " + m.group(1) + " -> " + m.group(2) + " @" + str(m.start()) + ":" + str(m.end()))
    p = re.compile("\\d+")
    print("dyn search: " + p.search("ab 42 cd").group(0))
    print("dyn findall: " + ",".join(p.findall("1 22 333")))
    print("dyn sub: " + p.sub("N", "a1b22"))
    q = re.compile("(cat|dog)")
    m2 = q.search("a cat and a dog", 6)
    if m2:
        print("pos search: " + m2.group(0) + " @" + str(m2.start()))
    # a pattern built at runtime: no constant to intern, so this exercises
    # the dynamic compile path rather than the specialized matcher
    word = "dog"
    dyn = re.compile("(" + word + ")s?")
    md = dyn.search("hotdogs")
    if md:
        print("runtime-built: " + md.group(0) + " @" + str(md.start()))
    n = 0
    for m3 in q.finditer("cat dog cat"):
        n = n + 1
    print("finditer count: " + str(n))
main()
