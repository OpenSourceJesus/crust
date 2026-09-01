"""The --mem-safe runtime tier: flag plumbing, detection, and the guarantee
that the identical source rebuilds clean without the flag.

The last one is the property the whole feature rests on -- if a release build
of instrumented source failed to compile, or quietly kept the runtime linked,
the flag would be useless for its stated workflow (test with it, ship without
it). It is checked here rather than assumed.
"""
import os
import subprocess
import tempfile
import unittest


def _build(src, flags):
    """Compile `src` with `flags`; return (path, "") or (None, diagnostics)."""
    d = tempfile.mkdtemp()
    c = os.path.join(d, "t.c")
    with open(c, "w") as f:
        f.write(src)
    out = os.path.join(d, "t")
    p = subprocess.run(["shivyc", "--no-cache"] + flags + [c, "-o", out],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None, p.stdout + p.stderr
    return out, ""


def _run(src, flags=("--mem-safe",)):
    """Build and run; return (exit status, stderr) or (None, diagnostics)."""
    out, err = _build(src, list(flags))
    if out is None:
        return None, err
    p = subprocess.run([out], capture_output=True, text=True)
    return p.returncode, p.stderr


PROLOGUE = '#include <stdlib.h>\n#include "crust_memsafe.h"\n'


class TestMemSafeFlag(unittest.TestCase):
    def test_bare_flag_does_not_eat_the_input_file(self):
        # --mem-safe takes an optional level, and a greedy parser reads the
        # next word as that level -- consuming the source file and failing for
        # want of an input. Pins the normalisation that prevents it.
        out, err = _build(PROLOGUE + "int main(void){return 0;}\n",
                          ["--mem-safe"])
        self.assertIsNotNone(out, err)

    def test_unknown_level_is_rejected(self):
        out, err = _build("int main(void){return 0;}\n", ["--mem-safe=bogus"])
        self.assertIsNone(out)
        self.assertIn("--mem-safe level", err)

    def test_cpp_level_accepted(self):
        out, err = _build(PROLOGUE + "int main(void){return 0;}\n",
                          ["--mem-safe=cpp"])
        self.assertIsNotNone(out, err)

    def test_header_resolves_without_the_flag(self):
        # The runtime directory is on the include path in *both* builds. If it
        # were added only under --mem-safe, the release build -- the one that
        # ships -- would be the only one that failed to compile.
        out, err = _build(PROLOGUE + "int main(void){return 0;}\n", [])
        self.assertIsNotNone(out, err)


class TestMemSafeDetection(unittest.TestCase):
    def _assert_reports(self, src, phrase):
        rc, err = _run(PROLOGUE + src)
        self.assertIsNotNone(rc, err)
        self.assertIn(phrase, err)
        self.assertEqual(rc, 1, "a run that reported errors must exit 1")

    def test_heap_overflow(self):
        self._assert_reports(
            "int main(void){\n"
            "  int *a = (int *)CRUST_MS_MALLOC(4*sizeof(int));\n"
            "  CRUST_MS_WR(&a[4], int, \"a[4]\") = 1;\n"
            "  CRUST_MS_FREE(a); return 0; }\n",
            "heap buffer overflow")

    def test_use_after_free(self):
        self._assert_reports(
            "int main(void){\n"
            "  int *p = (int *)CRUST_MS_MALLOC(sizeof(int));\n"
            "  CRUST_MS_WR(p, int, \"*p\") = 1;\n"
            "  CRUST_MS_FREE(p);\n"
            "  return CRUST_MS_RD(p, int, \"*p\") ? 0 : 0; }\n",
            "use after free")

    def test_double_free(self):
        self._assert_reports(
            "int main(void){\n"
            "  char *s = (char *)CRUST_MS_MALLOC(8);\n"
            "  CRUST_MS_FREE(s); CRUST_MS_FREE(s); return 0; }\n",
            "double free")

    def test_uninitialized_read(self):
        self._assert_reports(
            "int main(void){\n"
            "  int *p = (int *)CRUST_MS_MALLOC(2*sizeof(int));\n"
            "  CRUST_MS_WR(&p[0], int, \"p[0]\") = 1;\n"
            "  return CRUST_MS_RD(&p[1], int, \"p[1]\") ? 0 : 0; }\n",
            "uninitialized")

    def test_reports_the_allocation_site_not_just_the_access(self):
        # Naming where the object came from is most of the diagnostic's value:
        # the bug is usually at the allocation, and the access is only where it
        # surfaced.
        rc, err = _run(PROLOGUE +
                       "int main(void){\n"
                       "  int *a = (int *)CRUST_MS_MALLOC(4);\n"
                       "  CRUST_MS_WR(&a[9], int, \"a[9]\") = 1;\n"
                       "  CRUST_MS_FREE(a); return 0; }\n")
        self.assertIsNotNone(rc, err)
        # Two lines of prologue, then main at 3, the malloc at 4, the bad
        # write at 5. Both sites must appear, and they must be different --
        # a report that blamed the allocation line for the access would pass
        # a laxer check.
        self.assertIn("t.c:5", err)      # the access
        self.assertIn("allocated at", err)
        self.assertIn("t.c:4", err)      # the allocation

    def test_calloc_is_defined_but_malloc_is_not(self):
        rc, err = _run(PROLOGUE +
                       "int main(void){\n"
                       "  int *p = (int *)CRUST_MS_CALLOC(2, sizeof(int));\n"
                       "  int v = CRUST_MS_RD(&p[1], int, \"p[1]\");\n"
                       "  CRUST_MS_FREE(p); return v; }\n")
        self.assertIsNotNone(rc, err)
        self.assertNotIn("uninitialized", err)

    def test_clean_program_is_clean(self):
        rc, err = _run(PROLOGUE +
                       "int main(void){\n"
                       "  int *a = (int *)CRUST_MS_MALLOC(4*sizeof(int));\n"
                       "  int i, s = 0;\n"
                       "  for (i = 0; i < 4; i++) "
                       "CRUST_MS_WR(&a[i], int, \"a[i]\") = i;\n"
                       "  for (i = 0; i < 4; i++) "
                       "s += CRUST_MS_RD(&a[i], int, \"a[i]\");\n"
                       "  CRUST_MS_FREE(a); return s == 6 ? 0 : 2; }\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("clean", err)


class TestReleaseBuild(unittest.TestCase):
    BUGGY = (PROLOGUE +
             "int main(void){\n"
             "  int *a = (int *)CRUST_MS_MALLOC(4*sizeof(int));\n"
             "  CRUST_MS_WR(&a[0], int, \"a[0]\") = 7;\n"
             "  CRUST_MS_FREE(a); return 0; }\n")

    def test_release_build_links_no_runtime(self):
        out, err = _build(self.BUGGY, [])
        self.assertIsNotNone(out, err)
        syms = subprocess.run(["nm", out], capture_output=True, text=True)
        self.assertNotIn("crust_ms_check_read", syms.stdout)

    def test_checked_build_does_link_the_runtime(self):
        out, err = _build(self.BUGGY, ["--mem-safe"])
        self.assertIsNotNone(out, err)
        syms = subprocess.run(["nm", out], capture_output=True, text=True)
        self.assertIn("crust_ms_check_read", syms.stdout)

    def test_release_build_is_silent(self):
        out, err = _build(self.BUGGY, [])
        self.assertIsNotNone(out, err)
        p = subprocess.run([out], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("--mem-safe", p.stderr)


if __name__ == "__main__":
    unittest.main()
