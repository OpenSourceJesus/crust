"""The C++ tier: `--mem-safe=cpp` and `cpprust.py --mem-safe`.

The point of the level is selectivity. A project with audited C and a newer C++
layer wants the C++ checked and the C left at full speed, and both end up in the
same translation unit because a `.cpp` is lowered and spliced in by the
preprocessor. What separates them by then is the file name on each command's
source range -- so these tests are mostly about *what is not* instrumented.

`cpprust.py --mem-safe` covers the standalone case. Run separately the lowering
throws the origin away and the generated C looks like C, so the flag re-emits
cpprust's own origin anchors as `#line` directives.
"""
import os
import subprocess
import tempfile
import unittest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CPPRUST = os.path.join(REPO, "tools", "cpprust.py")

LIB_CPP = """\
struct Buf {
    int d[4];
    int get(int i) { return d[i]; }
    void put(int i, int v) { d[i] = v; }
};
extern "C" int cpp_bug(void) {
    Buf b;
    for (int i = 0; i < 4; i++) b.put(i, i);
    return b.get(7);
}
"""

APP_C = """\
#include "lib.cpp"
static int c_side(void) {
    int a[4]; int i, s = 0;
    for (i = 0; i < 4; i++) a[i] = i;
    for (i = 0; i < 4; i++) s += a[i];
    return s;
}
int main(void) { return c_side() + cpp_bug(); }
"""


def _project():
    """A directory holding a C++ unit and the C that includes it."""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "lib.cpp"), "w") as f:
        f.write(LIB_CPP)
    with open(os.path.join(d, "app.c"), "w") as f:
        f.write(APP_C)
    return d


def _build(d, flags, out_name):
    out = os.path.join(d, out_name)
    p = subprocess.run(["shivyc", "--no-cache"] + list(flags)
                       + [os.path.join(d, "app.c"), "-o", out],
                       capture_output=True, text=True, cwd=d)
    if p.returncode != 0:
        return None, p.stdout + p.stderr
    return out, p.stdout


class TestCppTier(unittest.TestCase):
    def test_cpp_bug_is_caught(self):
        d = _project()
        out, info = _build(d, ["--mem-safe=cpp"], "app_cpp")
        self.assertIsNotNone(out, info)
        p = subprocess.run([out], capture_output=True, text=True)
        self.assertIn("stack buffer overflow", p.stderr)
        self.assertIn("lib.cpp", p.stderr)
        self.assertEqual(p.returncode, 1)

    def test_cpp_tier_instruments_less_than_the_c_tier(self):
        # Same source, both levels. `all` checks the C loop too; `cpp` does
        # not. If these ever came out equal the level would be doing nothing.
        d = _project()
        _, info_cpp = _build(d, ["--mem-safe=cpp"], "a1")
        _, info_all = _build(d, ["--mem-safe"], "a2")

        def emitted(info):
            for tok in info.replace(",", " ").split():
                if tok.isdigit():
                    return int(tok)
            return -1

        self.assertGreater(emitted(info_all), emitted(info_cpp))

    def test_pure_c_gets_nothing_from_the_cpp_tier(self):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write("int main(void){ int a[4]; a[9] = 1; return a[0]; }\n")
        out = os.path.join(d, "t")
        p = subprocess.run(["shivyc", "--no-cache", "--mem-safe=cpp", c,
                            "-o", out], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0 check(s) emitted", p.stdout)


class TestCpprustMemSafeFlag(unittest.TestCase):
    def test_flag_emits_line_directives(self):
        d = _project()
        gen = os.path.join(d, "lib_gen.c")
        p = subprocess.run(["python3", CPPRUST, "--mem-safe",
                            os.path.join(d, "lib.cpp"), "-o", gen,
                            "--basedir", d], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(gen) as f:
            text = f.read()
        self.assertIn('#line 1 "', text)
        self.assertIn("lib.cpp", text)

    def test_without_the_flag_no_line_directives(self):
        d = _project()
        gen = os.path.join(d, "lib_plain.c")
        p = subprocess.run(["python3", CPPRUST,
                            os.path.join(d, "lib.cpp"), "-o", gen,
                            "--basedir", d], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(gen) as f:
            self.assertNotIn("#line", f.read())

    def test_standalone_output_is_checked_and_the_driver_is_not(self):
        # The whole reason the flag exists: compiled separately, the generated
        # C would look like C and the tier would find nothing to check.
        d = _project()
        gen = os.path.join(d, "lib_gen.c")
        subprocess.run(["python3", CPPRUST, "--mem-safe",
                        os.path.join(d, "lib.cpp"), "-o", gen,
                        "--basedir", d], capture_output=True, text=True)
        drv = os.path.join(d, "drv.c")
        with open(drv, "w") as f:
            f.write("int cpp_bug(void);\n"
                    "static int c_side(void){ int a[4]; int i, s = 0;\n"
                    "  for (i=0;i<4;i++) a[i]=i;\n"
                    "  for (i=0;i<4;i++) s+=a[i]; return s; }\n"
                    "int main(void){ return c_side() + cpp_bug(); }\n")
        out = os.path.join(d, "prog")
        p = subprocess.run(["shivyc", "--no-cache", "--mem-safe=cpp",
                            gen, drv, "-o", out],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("0 check(s) emitted", p.stdout)      # drv.c
        run = subprocess.run([out], capture_output=True, text=True)
        self.assertIn("stack buffer overflow", run.stderr)
        self.assertIn("lib.cpp", run.stderr)


if __name__ == "__main__":
    unittest.main()
