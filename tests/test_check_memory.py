"""The static tier (--check-memory), and specifically the source locations it
reports.

IL commands used to carry no positions, so this pass could only say
"[use-after-free] in main". It now names the use, the allocation, and the free.
The allocation site is the one that matters most -- for an alias bug the use is
just where it surfaced, and the fix is at the malloc.

These also serve as regression tests for the IL range plumbing itself: if
ILCode.add ever stops stamping, or a front-end funnel stops setting a range,
the line numbers here go wrong or vanish.
"""
import os
import subprocess
import tempfile
import unittest


def _check(src):
    """Run --check-memory over `src`; return its report."""
    d = tempfile.mkdtemp()
    c = os.path.join(d, "t.c")
    with open(c, "w") as f:
        f.write(src)
    p = subprocess.run(["shivyc", "--no-cache", c, "--check-memory"],
                       capture_output=True, text=True)
    return p.stdout + p.stderr


class TestCheckMemoryLocations(unittest.TestCase):
    def test_double_free_names_all_three_sites(self):
        report = _check(
            "#include <stdlib.h>\n"          # 1
            "int main(void) {\n"             # 2
            "    int *p = malloc(4);\n"      # 3  allocation
            "    free(p);\n"                 # 4  first free
            "    free(p);\n"                 # 5  the double free
            "    return 0;\n"
            "}\n")
        self.assertIn("double-free", report)
        self.assertIn("t.c:5", report)                  # the offending free
        self.assertIn("allocated at", report)
        self.assertIn("t.c:3", report)                  # the allocation
        self.assertIn("freed at", report)
        self.assertIn("t.c:4", report)                  # the first free

    def test_use_after_free_through_an_alias(self):
        # The use is at line 6 but the bug is the free at 5 of an object
        # allocated at 3 -- the case where naming only the use is least useful.
        report = _check(
            "#include <stdlib.h>\n"          # 1
            "int main(void) {\n"             # 2
            "    int *d = malloc(4);\n"      # 3
            "    int *a = d;\n"              # 4
            "    free(d);\n"                 # 5
            "    return *a;\n"               # 6
            "}\n")
        self.assertIn("use-after-free", report)
        self.assertIn("t.c:6", report)
        self.assertIn("t.c:3", report)
        self.assertIn("t.c:5", report)

    def test_free_site_is_the_first_free_not_the_second(self):
        # For a double free the site that made the pointer dangle is the
        # interesting one; the second is merely where it was noticed.
        report = _check(
            "#include <stdlib.h>\n"
            "int main(void) {\n"
            "    char *s = malloc(8);\n"     # 3
            "    free(s);\n"                 # 4
            "    free(s);\n"                 # 5
            "    return 0;\n"
            "}\n")
        line = [ln for ln in report.splitlines() if "freed at" in ln]
        self.assertTrue(line, report)
        self.assertIn("t.c:4", line[0])
        self.assertNotIn("t.c:5", line[0])

    def test_autofree_candidate_names_its_allocation(self):
        report = _check(
            "#include <stdlib.h>\n"
            "int main(void) {\n"
            "    int *a = malloc(4);\n"      # 3
            "    *a = 1;\n"
            "    return *a;\n"
            "}\n")
        self.assertIn("auto-free candidates", report)
        self.assertIn("allocated at", report)
        self.assertIn("t.c:3", report)

    def test_clean_program_reports_nothing(self):
        report = _check(
            "#include <stdlib.h>\n"
            "int main(void) {\n"
            "    int *p = malloc(4);\n"
            "    *p = 3;\n"
            "    int v = *p;\n"
            "    free(p);\n"
            "    return v - 3;\n"
            "}\n")
        self.assertIn("no use-after-free or double-free", report)


class TestILRanges(unittest.TestCase):
    """Direct checks on the range plumbing, independent of any one pass."""

    def _il(self, src):
        import shivyc.main as M
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(src)
        seen = []
        orig = M.ILCode.add

        def add(self_, cmd):
            orig(self_, cmd)
            seen.append(cmd)

        M.ILCode.add = add
        try:
            import sys
            argv = sys.argv
            sys.argv = ["shivyc", "--no-cache", "-S", c,
                        "-o", os.path.join(d, "t.s")]
            try:
                M.main()
            except SystemExit:
                pass
            finally:
                sys.argv = argv
        finally:
            M.ILCode.add = orig
        return seen

    def test_every_command_gets_a_position(self):
        cmds = self._il(
            "#include <stdlib.h>\n"
            "int add2(int a, int b) { return a + b; }\n"
            "int main(void) {\n"
            "    int *p = malloc(8);\n"
            "    p[0] = 1;\n"
            "    return add2(p[0], 2);\n"
            "}\n")
        self.assertTrue(cmds)
        missing = [type(c).__name__ for c in cmds if c.r is None]
        self.assertEqual(missing, [], "IL commands with no source range")

    def test_positions_track_the_statement(self):
        cmds = self._il(
            "#include <stdlib.h>\n"          # 1
            "int main(void) {\n"             # 2
            "    int *p = malloc(8);\n"      # 3
            "    p[0] = 1;\n"                # 4
            "    return p[0];\n"             # 5
            "}\n")
        import shivyc.il_cmds.value as value_cmds
        writes = [c for c in cmds if isinstance(c, value_cmds.SetAt)]
        reads = [c for c in cmds if isinstance(c, value_cmds.ReadAt)]
        self.assertTrue(writes and reads)
        self.assertEqual(writes[0].r.start.line, 4)
        self.assertEqual(reads[0].r.start.line, 5)


if __name__ == "__main__":
    unittest.main()
