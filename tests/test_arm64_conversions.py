"""Integer conversions on the arm64 back end, differentially against gcc.

Every pair of char, unsigned char, short, unsigned short, int, unsigned,
long, unsigned long and _Bool, over runtime values that cross each type's
boundaries (1539 conversions), compiled by Crust and by aarch64 gcc and run
under qemu; the printed results must match exactly.

The arm64 back end held a char/short/_Bool in a 32-bit register without
making it canonical: narrowing into a register was a plain `mov`, and
widening assumed the value was already extended. 795 of these 1539
conversions were wrong -- `(unsigned char)-1` came out 4294967295,
`(_Bool)2` stayed 2. (The x86-64 back end got them all right.)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TYPES = ["signed char", "unsigned char", "short", "unsigned short", "int",
         "unsigned int", "long", "unsigned long", "_Bool"]
VALUES = ["0", "1", "-1", "127", "128", "255", "256", "-129", "32767",
          "32768", "65535", "65536", "70000", "-70000", "2147483647",
          "-2147483648L", "4294967295L", "4294967296L", "-4294967297L"]


def _program():
    out = ["#include <stdio.h>",
           "static long seed(long v) { return v; }  /* defeat folding */",
           "int main(void) {", "  long v; int n = 0;"]
    for v in VALUES:
        out.append("  v = seed(%s);" % v)
        for src in TYPES:
            out.append("  { %s a = (%s)v;" % (src, src))
            for dst in TYPES:
                out.append('    { %s b = (%s)a; printf("%%d %%ld\\n", n++,'
                           ' (long)b); }' % (dst, dst))
            out.append("  }")
    out.append("  return 0; }")
    return "\n".join(out) + "\n"


@unittest.skipUnless(shutil.which("aarch64-linux-gnu-gcc")
                     and shutil.which("qemu-aarch64"),
                     "needs aarch64-linux-gnu-gcc and qemu-aarch64")
class Arm64ConversionTests(unittest.TestCase):

    def test_all_integer_conversions_match_gcc(self):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "conv.c")
        with open(c, "w") as f:
            f.write(_program())
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env.setdefault("QEMU_LD_PREFIX", "/usr/aarch64-linux-gnu")
        s = os.path.join(d, "conv.s")
        p = subprocess.run([sys.executable, "-m", "shivyc.main", c, "-S",
                            "-o", s, "--target", "arm64", "--os", "linux"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        runs = {}
        for who, src in (("crust", s), ("gcc", c)):
            exe = os.path.join(d, who)
            subprocess.run(["aarch64-linux-gnu-gcc", "-O0", src, "-o", exe],
                           check=True, capture_output=True)
            runs[who] = subprocess.run(["qemu-aarch64", exe], env=env,
                                       capture_output=True, text=True,
                                       timeout=120).stdout.splitlines()
        self.assertEqual(len(runs["gcc"]), len(VALUES) * len(TYPES) ** 2)
        wrong = [(g, m) for g, m in zip(runs["gcc"], runs["crust"]) if g != m]
        self.assertEqual(wrong[:10], [], "%d conversions differ" % len(wrong))


if __name__ == "__main__":
    unittest.main()
