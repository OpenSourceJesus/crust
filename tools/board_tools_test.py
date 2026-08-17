"""Test the board driver scripts (raspi.py, jetnano.py) end to end.

These are the tools a user reaches for first, so a break in them is very
visible. Covers both build modes, the qemu run, the register capture, and both
outcomes of the test-script hook -- a passing script must pass and a failing
one must actually fail, which is the part most likely to rot silently.

    python3 tools/board_tools_test.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PROG = """
int printf(const char *fmt, ...);
int main(void){ printf("board %d\\n", 42); return 7; }
"""

PASSING = """
def check(rec):
    if rec.exit_code != 7:
        return "expected 7, got %d" % rec.exit_code
    if "board 42" not in rec.stdout:
        return "printf output missing: %r" % rec.stdout
"""

FAILING = """
def check(rec):
    return "deliberate failure"
"""

REGISTERS = """
def check(rec):
    x0 = rec.registers.get("X00")
    if x0 is None:
        return "no registers captured"
    if x0 & 0xFF != 7:
        return "X0 low byte %#x, expected 7" % (x0 & 0xFF)
"""


def run(cmd, timeout=900):
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT,
                       timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def main(argv):
    wd = tempfile.mkdtemp(prefix="boardtools-")
    src = os.path.join(wd, "prog.c")
    open(src, "w").write(PROG)
    for name, body in (("pass.py", PASSING), ("fail.py", FAILING),
                       ("regs.py", REGISTERS)):
        open(os.path.join(wd, name), "w").write(body)

    npass = nfail = 0

    def check(label, ok, detail=""):
        nonlocal npass, nfail
        if ok:
            npass += 1
            print("  PASS  %s" % label)
        else:
            nfail += 1
            print("  FAIL  %s%s" % (label, (" -- " + detail) if detail else ""))

    for tool in ("raspi.py", "jetnano.py"):
        path = os.path.join(HERE, tool)
        print("\n== %s ==" % tool)

        rc, out = run([sys.executable, path, "--info"])
        check("--info", rc == 0 and "AArch64" in out)

        outdir = os.path.join(wd, tool + ".selfhosted")
        rc, out = run([sys.executable, path, src, "--qemu",
                       "--out=" + outdir])
        ok = rc == 0 and "board 42" in out and "exit 7" in out
        check("build + run (self-hosted)", ok, out.strip()[-120:])
        check("packaged", os.path.exists(os.path.join(outdir, "manifest.json"))
              and os.path.exists(os.path.join(outdir, "run-on-board.sh")))

        rc, out = run([sys.executable, path, src,
                       "--test-script=" + os.path.join(wd, "pass.py"),
                       "--out=" + os.path.join(wd, tool + ".p")])
        check("passing test script exits 0", rc == 0 and "PASS" in out,
              out.strip()[-120:])

        rc, out = run([sys.executable, path, src,
                       "--test-script=" + os.path.join(wd, "fail.py"),
                       "--out=" + os.path.join(wd, tool + ".f")])
        # The important half: a failing script must fail the run, or the hook
        # is decorative.
        check("failing test script exits nonzero",
              rc != 0 and "deliberate failure" in out, out.strip()[-120:])

        rc, out = run([sys.executable, path, src, "--debug",
                       "--test-script=" + os.path.join(wd, "regs.py"),
                       "--out=" + os.path.join(wd, tool + ".d")])
        check("--debug captures registers", rc == 0 and "regs at exit" in out,
              out.strip()[-120:])

    print("\nboard tools: %d pass, %d fail" % (npass, nfail))
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
