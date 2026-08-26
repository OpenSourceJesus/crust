import re
def main():
    m = re.search("(\\w+)=(\\d+)", "x port=8080 y")
    if m:
        print("g1=" + m.group(1) + " s=" + str(m.start()) + " e=" + str(m.end()))
        print("s1=" + str(m.start(1)) + " e2=" + str(m.end(2)))
main()
