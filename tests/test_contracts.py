"""Compile-time function contracts (`assert len(arg) <= N` style clauses).

Verifies that a precondition is reported as a compile error at any call site
where the argument's length is statically known and provably violates the
bound, that the diagnostic names the caller/argument/callee/clause, and -- just
as importantly -- that the check never fires when the length is unknown or the
bound is satisfied (no false positives), and that valid programs still run.
"""
import os
import subprocess
import tempfile
import unittest


def _compile(src, link=True):
    """Compile one C source string. Returns (proc, output_path)."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "contract.c")
    with open(path, "w") as f:
        f.write(src)
    out = os.path.join(d, "prog")
    args = ["shivyc", "--no-cache", path, "-o", out]
    proc = subprocess.run(args, capture_output=True, text=True, cwd=d)
    return proc, out


# A strcpy prototype so the bodies that use it do not trip "undeclared".
PROTO = "char *strcpy(char *dest, char *src);\n"


class TestContracts(unittest.TestCase):
    """Static checking of function preconditions."""

    def test_violation_via_variable(self):
        """A variable holding a too-long string is reported, with the
        caller, argument, callee and clause all named."""
        src = PROTO + (
            "void process_input(char *user_input)\n"
            "    assert len(user_input) <= 16\n"
            "{ char buffer[16]; strcpy(buffer, user_input); }\n"
            "int main(){\n"
            '    char *large_string = "ThisInputIsWayTooLongForTheBuffer";\n'
            "    process_input(large_string);\n"
            "    return 0;\n}\n")
        proc, _ = _compile(src, link=False)
        self.assertNotEqual(proc.returncode, 0)
        msg = proc.stdout + proc.stderr
        self.assertIn("in the function `main`", msg)
        self.assertIn("char * large_string", msg)
        self.assertIn("process_input", msg)
        self.assertIn("assert len(user_input) <= 16", msg)

    def test_violation_via_string_literal(self):
        """A too-long string literal passed directly is reported."""
        src = (
            "void f(char *p) assert len(p) <= 4 { (void)p; }\n"
            'int main(){ f("toolong"); return 0; }\n')
        proc, _ = _compile(src, link=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("too large", proc.stdout + proc.stderr)

    def test_len_lower_bound(self):
        """`len(p) >= N` reports a too-short argument."""
        src = (
            "void f(char *p) assert len(p) >= 64 { (void)p; }\n"
            'int main(){ char *s = "tiny"; f(s); return 0; }\n')
        proc, _ = _compile(src, link=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("too small", proc.stdout + proc.stderr)

    def test_divisibility_bound(self):
        """`assert not len(p) % N` reports a non-multiple length."""
        src = (
            "void f(char *p) assert not len(p) % 4 { (void)p; }\n"
            'int main(){ char *s = "abcde"; f(s); return 0; }\n')
        proc, _ = _compile(src, link=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("len(p) % 4", proc.stdout + proc.stderr)

    def test_within_bound_ok(self):
        """A satisfied contract compiles and the program runs."""
        src = (
            "void f(char *p) assert len(p) <= 16 { (void)p; }\n"
            'int main(){ char *s = "fine"; f(s); return 0; }\n')
        proc, out = _compile(src)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        run = subprocess.run([out])
        self.assertEqual(run.returncode, 0)

    def test_no_false_positive_unknown_length(self):
        """When the argument length is not statically known, the contract is
        not checked -- no spurious compile error."""
        src = (
            "char *get(void){ return 0; }\n"
            "void f(char *p) assert len(p) <= 4 { (void)p; }\n"
            "int main(){ f(get()); return 0; }\n")
        proc, _ = _compile(src, link=False)
        # There must be no contract error; the program is accepted.
        self.assertNotIn("because of the contract",
                         proc.stdout + proc.stderr)

    def test_reassignment_to_shorter_clears_violation(self):
        """Reassigning a tracked variable to a shorter string before the call
        means no violation is reported."""
        src = (
            "void f(char *p) assert len(p) <= 4 { (void)p; }\n"
            'int main(){ char *s = "waytoolong"; s = "ok";'
            " f(s); return 0; }\n")
        proc, _ = _compile(src, link=False)
        self.assertNotIn("because of the contract",
                         proc.stdout + proc.stderr)

    def test_reassignment_to_longer_is_caught(self):
        """Reassigning a tracked variable to a too-long string before the call
        is caught."""
        src = (
            "void f(char *p) assert len(p) <= 4 { (void)p; }\n"
            'int main(){ char *s = "ok"; s = "waytoolong";'
            " f(s); return 0; }\n")
        proc, _ = _compile(src, link=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("too large", proc.stdout + proc.stderr)

    def test_no_contract_is_unaffected(self):
        """Ordinary functions without contracts are entirely unaffected."""
        src = (
            "int add(int a, int b){ return a + b; }\n"
            "int main(){ return add(2, 3) == 5 ? 0 : 1; }\n")
        proc, out = _compile(src)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        run = subprocess.run([out])
        self.assertEqual(run.returncode, 0)

    def test_call_in_condition_not_a_contract_region(self):
        # A bare function CALL nested in an if-condition (e.g. CPython's
        # `if (!track && maybe_tracked(o)) {`) must not be mistaken for a
        # contract region by the extension pre-pass, even when an unrelated
        # contract appears elsewhere in the file.
        src = (
            "int helper(int x) assert len(x) >= 0 { return x; }\n"
            "static int maybe_tracked(int o){ return o & 1; }\n"
            "int main(){\n"
            "  int track = 0;\n"
            "  int o = 3;\n"
            "  if (!track && maybe_tracked(o)) { track = 1; }\n"
            "  return track;\n"
            "}\n")
        proc, _ = _compile(src, link=False)
        # Must compile without an 'extension region' error (helper's contract
        # over an int has no len provenance, so no violation is reported).
        self.assertNotIn("extension region", proc.stdout + proc.stderr)

    def test_name_in_comment_not_an_extension_region(self):
        # A function-name-like token inside a comment (e.g. CPython's
        # `/* ... _PyDict_CheckConsistency() */`) must not be mistaken for a
        # function-definition header whose "region" runs across later code.
        src = (
            "/* Uncomment to check content in checkit() */\n"
            "#if 0\n"
            "#  define ASSERT_C(op) myassert(checkit((op), 1))\n"
            "#endif\n"
            "int checkit(int *op, int n){\n"
            "  if (!op) return -1;   /* assert-like check */\n"
            "  return n;\n"
            "}\n"
            "int main(){ int x=5; return checkit(&x, 7); }\n")
        proc, _ = _compile(src, link=False)
        self.assertNotIn("extension region", proc.stdout + proc.stderr)


    def test_second_clause_is_checked_too(self):
        """A length clearing one clause but breaking another is reported.

        `assert len(p) >= 4` and `assert not len(p) % 4` are two clauses of one
        contract. A seven-character literal clears the first and breaks the
        second. The reading used to stop at the first clause present, so this
        compiled; `simd_contracts` meanwhile conjoined every clause and refused
        to prove the same call, which is how the split was found.
        """
        src = PROTO + (
            "void handle(char *p)\n"
            "    assert len(p) >= 4\n"
            "    assert not len(p) % 4\n"
            "{ char buf[64]; strcpy(buf, p); }\n"
            "int main(void) { handle(\"abcdefg\"); return 0; }\n")
        proc, _ = _compile(src)
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not len(p) % 4", out)

    def test_all_clauses_satisfied_still_compiles(self):
        """The same contract with a length meeting every clause is fine."""
        src = PROTO + (
            "void handle(char *p)\n"
            "    assert len(p) >= 4\n"
            "    assert not len(p) % 4\n"
            "{ char buf[64]; strcpy(buf, p); }\n"
            "int main(void) { handle(\"abcdefgh\"); return 0; }\n")
        proc, out = _compile(src)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_two_passes_read_one_contract(self):
        """`contracts` and `simd_contracts` agree on every clause set."""
        from shivyc.contracts import _violates
        from shivyc.simd_contracts import _satisfies
        grid = [{"len>=": 64}, {"len<=": 8}, {"div-by": 4},
                {"len>=": 64, "div-by": 4}, {"len>=": 4, "len<=": 16},
                {"len>=": 8, "len<=": 64, "div-by": 8}]
        for bound in grid:
            for n in range(0, 130):
                self.assertEqual(_violates(n, bound), not _satisfies(n, bound),
                                 f"{bound} at length {n}")

    def test_an_unreadable_clause_is_an_error(self):
        """A clause with no reading is raised, not skipped."""
        from shivyc import proofs
        with self.assertRaises(proofs.UnknownBound):
            proofs.satisfies(64, {"align": 16})


if __name__ == "__main__":
    unittest.main()
