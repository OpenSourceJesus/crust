import re
def main():
    m = re.search("(\\w+)=(\\d+)", "x port=8080 y")
    if m:
        print("g0=" + m.group(0) + " s=" + str(m.start()) + " e=" + str(m.end()))
        print("g1=" + m.group(1) + " s1=" + str(m.start(1)) + " e1=" + str(m.end(1)))
        print("g2=" + m.group(2) + " s2=" + str(m.start(2)) + " e2=" + str(m.end(2)))
    m2 = re.search("^\\d+", "12345")
    if m2:
        print("tier1 s=" + str(m2.start()) + " e=" + str(m2.end()))
main()
