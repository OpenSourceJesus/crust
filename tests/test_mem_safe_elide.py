"""Check elision: dropping --mem-safe checks the compiler can prove unnecessary.

The overwhelming majority of these tests are soundness tests, and deliberately
so. An elision bug does not announce itself -- the build succeeds, the program
runs, and a real bug goes unreported, which looks exactly like a clean program.
Every class the checker detects is therefore re-tested here through code shaped
to trigger elision, so a wrong proof fails a test instead of quietly costing
someone a bug.

Two rules are under test:
  1. redundancy -- the identical address was already checked in this block
  2. bounds -- a constant offset into a live allocation of known size

Rule 2 is the subtle one. A write check also records which bytes are defined,
so a proved write is downgraded to a shadow update rather than removed. The
first implementation removed it outright and manufactured a false
"uninitialized read" on the very next line; test_proved_write_still_marks_bytes
pins that.
"""
import os
import subprocess
import tempfile
import unittest


def _build(src, flags=("--mem-safe",)):
    d = tempfile.mkdtemp()
    c = os.path.join(d, "t.c")
    with open(c, "w") as f:
        f.write(src)
    out = os.path.join(d, "t")
    p = subprocess.run(["shivyc", "--no-cache"] + list(flags) + [c, "-o", out],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None, p.stdout + p.stderr
    return out, p.stdout


def _run(src):
    out, info = _build(src)
    if out is None:
        return None, info, info
    p = subprocess.run([out], capture_output=True, text=True)
    return p.returncode, p.stderr, info


STRUCT = ("#include <stdlib.h>\n"
          "struct s { int a; int b; };\n")


class TestElisionIsSound(unittest.TestCase):
    """Bugs must still be caught in code shaped to trigger elision."""

    def _assert_caught(self, src, phrase):
        rc, err, info = _run(src)
        self.assertIsNotNone(rc, info)
        self.assertIn(phrase, err)
        self.assertEqual(rc, 1)

    def test_use_after_free_through_a_field(self):
        # A constant offset into a known-size allocation -- exactly rule 2's
        # shape. The allocation is dead by the time it is read, so the proof
        # must not apply.
        self._assert_caught(
            STRUCT +
            "int main(void){ struct s *p = malloc(sizeof(struct s));\n"
            "  p->a = 1; p->b = 2; free(p); return p->a; }\n",
            "use after free")

    def test_uninitialized_field_read(self):
        self._assert_caught(
            STRUCT +
            "int main(void){ struct s *p = malloc(sizeof(struct s));\n"
            "  p->a = 1; return p->b; }\n",
            "uninitialized")

    def test_overflow_in_a_read_modify_write_loop(self):
        # The write and the read share an address, so rule 1 drops one of the
        # two checks. The surviving one still has to catch the overrun.
        self._assert_caught(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(4*sizeof(int));\n"
            "  int i, s = 0;\n"
            "  for (i = 0; i < 6; i++) { a[i] = i; s += a[i]; }\n"
            "  free(a); return s; }\n",
            "heap buffer overflow")

    def test_offset_past_a_known_size_is_not_proved(self):
        # Constant offset, known size, live allocation -- but out of range.
        # The arithmetic must reject it rather than wave it through.
        self._assert_caught(
            "#include <stdlib.h>\n"
            "int main(void){ int *p = malloc(8); p[0]=1; p[1]=2;\n"
            "  p[5] = 3; free(p); return 0; }\n",
            "heap buffer overflow")

    def test_free_between_two_accesses_invalidates_the_proof(self):
        # A call clears every accumulated fact, because it may free anything.
        self._assert_caught(
            STRUCT +
            "int main(void){ struct s *p = malloc(sizeof(struct s));\n"
            "  p->a = 1;\n"
            "  free(p);\n"
            "  p->b = 2;\n"
            "  return 0; }\n",
            "use after free")

    def test_back_edge_does_not_carry_a_stale_proof(self):
        # The access is above the free in the loop body, so a linear scan
        # would prove it live and be right on the first iteration and wrong on
        # the second. Nothing may survive a block boundary.
        self._assert_caught(
            STRUCT +
            "int main(void){ struct s *p = malloc(sizeof(struct s));\n"
            "  int i;\n"
            "  for (i = 0; i < 2; i++) { p->a = i; free(p); }\n"
            "  return 0; }\n",
            "use after free")

    def test_free_inside_a_callee_invalidates_the_proof(self):
        # The free is not visible at this call site as a `free`; it happens
        # one level down. Any call has to be treated as capable of freeing.
        self._assert_caught(
            STRUCT +
            "static void grab(struct s *q){ free(q); }\n"
            "int main(void){ struct s *p = malloc(sizeof(struct s));\n"
            "  p->a = 1; grab(p); p->b = 2; return 0; }\n",
            "use after free")

    def test_proved_write_still_marks_bytes(self):
        # The regression that motivated downgrading rule 2 instead of eliding:
        # dropping a proved write left the shadow ignorant of it, and the next
        # read of those bytes was reported as uninitialized.
        rc, err, info = _run(
            "#include <stdlib.h>\n"
            "struct n { int k; struct n *next; };\n"
            "int main(void){ struct n *h = 0; int i, s = 0;\n"
            "  for (i = 0; i < 20; i++) {\n"
            "    struct n *x = malloc(sizeof(struct n));\n"
            "    x->k = i; x->next = h; h = x; }\n"
            "  while (h) { struct n *x = h->next; s += h->k; free(h); h = x; }\n"
            "  return s == 190 ? 0 : 1; }\n")
        self.assertIsNotNone(rc, info)
        self.assertNotIn("uninitialized", err)
        self.assertEqual(rc, 0, err)


class TestLoopRanges(unittest.TestCase):
    """Rule 3: `a[i]` inside a counted loop, proved in bounds for every pass.

    The upper bound comes from the loop guard and the lower from how the
    counter is written. Both halves are needed, and each of these tests removes
    one of them.
    """

    def _assert_caught(self, src, phrase):
        rc, err, info = _run(src)
        self.assertIsNotNone(rc, info)
        self.assertIn(phrase, err)

    def test_counted_loop_over_its_own_array_is_proved(self):
        _, info = _build(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(1000*sizeof(int));\n"
            "  long i, s = 0;\n"
            "  for (i = 0; i < 1000; i++) { a[i] = (int)i; s += a[i]; }\n"
            "  free(a); return (int)(s & 1); }\n")
        self.assertIn("0 check(s) emitted", info)

    def test_guard_one_past_the_end_is_not_proved(self):
        # `i <= 10` over ten elements. Off by one in the direction that
        # matters: the proof arithmetic has to include the boundary iteration.
        self._assert_caught(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(10*sizeof(int)); int i, s = 0;\n"
            "  for (i = 0; i <= 10; i++) { a[i] = i; s += a[i]; }\n"
            "  free(a); return s; }\n",
            "heap buffer overflow")

    def test_negative_start_is_not_proved(self):
        # The guard still says i < 10, so an upper bound alone would wave this
        # through. Only the non-negativity half rejects it.
        self._assert_caught(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(10*sizeof(int)); int i, s = 0;\n"
            "  for (i = -2; i < 10; i++) { a[i] = i; s += a[i]; }\n"
            "  free(a); return s; }\n",
            "underflow")

    def test_shifted_index_is_not_proved(self):
        # a[i+1] with i < 10 reaches a[10]. The constant offset has to be
        # carried into the bound arithmetic, not dropped.
        self._assert_caught(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(40); int i, s = 0;\n"
            "  for (i = 0; i < 10; i++) a[i] = i;\n"
            "  for (i = 0; i < 10; i++) s += a[i+1];\n"
            "  free(a); return s; }\n",
            "heap buffer overflow")

    def test_variable_bound_is_not_proved(self):
        # The guard compares against a variable, so there is no number to do
        # the arithmetic with.
        self._assert_caught(
            "#include <stdlib.h>\n"
            "int main(void){ int n = 20; int *a = malloc(40); int i, s = 0;\n"
            "  for (i = 0; i < n; i++) { a[i] = i; s += a[i]; }\n"
            "  free(a); return s; }\n",
            "heap buffer overflow")

    def test_loop_longer_than_the_allocation_is_not_proved(self):
        self._assert_caught(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(4*sizeof(int)); int i, s = 0;\n"
            "  for (i = 0; i < 6; i++) { a[i] = i; s += a[i]; }\n"
            "  free(a); return s; }\n",
            "heap buffer overflow")

    def test_freed_inside_the_loop_is_not_proved(self):
        # In bounds, but not live on the second pass.
        self._assert_caught(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(40); int i;\n"
            "  for (i = 0; i < 2; i++) { a[i] = i; free(a); }\n"
            "  return 0; }\n",
            "use after free")


class TestUnderflow(unittest.TestCase):
    """An access before an object's base lands in no tracked region at all,
    so it is invisible unless the runtime also looks for an object starting
    just above the address."""

    def test_underflow_is_reported_with_a_negative_offset(self):
        rc, err, info = _run(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(40); a[-2] = 1;\n"
            "  return 0; }\n")
        self.assertIsNotNone(rc, info)
        self.assertIn("underflow", err)
        self.assertIn("before the start", err)


class TestElisionActuallyFires(unittest.TestCase):
    def test_read_modify_write_drops_a_check(self):
        _, info = _build(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(1000*sizeof(int));\n"
            "  long i, s = 0;\n"
            "  for (i = 0; i < 1000; i++) { a[i] = (int)i; s += a[i]; }\n"
            "  free(a); return (int)(s & 1); }\n")
        self.assertIn("1 removed", info)

    def test_constant_offset_write_is_downgraded(self):
        _, info = _build(
            STRUCT +
            "int main(void){ struct s *p = malloc(sizeof(struct s));\n"
            "  p->a = 1; p->b = 2; free(p); return 0; }\n")
        self.assertIn("downgraded", info)
        self.assertNotIn("0 downgraded", info)

    def test_unprovable_code_keeps_every_check(self):
        # A pointer of unknown provenance and a runtime index: nothing to
        # prove, and the pass should say so rather than guess.
        _, info = _build(
            "#include <stdlib.h>\n"
            "int get(int *p, int i){ return p[i]; }\n"
            "int main(void){ int *a = malloc(40); a[0]=1;\n"
            "  int v = get(a, 0); free(a); return v-1; }\n")
        self.assertIn("0 removed", info)


class TestSemanticsUnchanged(unittest.TestCase):
    PROG = ("#include <stdlib.h>\n#include <stdio.h>\n"
            "struct s { int a; int b; };\n"
            "int main(void){ struct s *p = malloc(sizeof(struct s));\n"
            "  int *a = malloc(50*sizeof(int)); int i, t = 0;\n"
            "  p->a = 3; p->b = 4;\n"
            "  for (i = 0; i < 50; i++) { a[i] = i; t += a[i]; }\n"
            "  t += p->a + p->b;\n"
            "  printf(\"%d\\n\", t); free(a); free(p);\n"
            "  return t == 1232 ? 0 : 1; }\n")

    def test_elided_build_agrees_with_release(self):
        rel, e1 = _build(self.PROG, [])
        chk, e2 = _build(self.PROG, ["--mem-safe"])
        self.assertIsNotNone(rel, e1)
        self.assertIsNotNone(chk, e2)
        a = subprocess.run([rel], capture_output=True, text=True)
        b = subprocess.run([chk], capture_output=True, text=True)
        self.assertEqual(a.stdout, b.stdout)
        self.assertEqual(a.returncode, b.returncode)


class TestStaticPassSeesPointerArithmetic(unittest.TestCase):
    """--check-memory gained this while the elision work needed it."""

    def _check(self, src):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(src)
        p = subprocess.run(["shivyc", "--no-cache", c, "--check-memory"],
                           capture_output=True, text=True)
        return p.stdout + p.stderr

    def test_use_after_free_through_a_field_is_reported(self):
        # Points-to used to die at the first offset computation, so this bug
        # written as `p->key` went unreported while `*p` was caught.
        report = self._check(
            "#include <stdlib.h>\n"
            "struct node { int key; struct node *next; };\n"
            "int main(void){ struct node *p = malloc(sizeof(struct node));\n"
            "  p->key = 1; free(p); return p->key; }\n")
        self.assertIn("use-after-free", report)

    def test_use_after_free_through_an_index_is_reported(self):
        report = self._check(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(40); a[0] = 1; free(a);\n"
            "  return a[2]; }\n")
        self.assertIn("use-after-free", report)


if __name__ == "__main__":
    unittest.main()


class TestHoisting(unittest.TestCase):
    """A proved write in a counted loop writes one contiguous run, so a single
    shadow update before the loop replaces one per iteration. Once bounds and
    liveness are proved away that update is the entire remaining cost."""

    def test_loop_shadow_update_is_hoisted(self):
        _, info = _build(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(1000*sizeof(int));\n"
            "  long i, s = 0;\n"
            "  for (i = 0; i < 1000; i++) { a[i] = (int)i; s += a[i]; }\n"
            "  free(a); return (int)(s & 1); }\n")
        self.assertIn("1 shadow update(s) hoisted", info)
        self.assertIn("0 check(s) emitted", info)

    def test_hoist_leaves_the_loop_body_empty_in_a_nest(self):
        # The preheader of an inner loop still sits inside the outer one, so a
        # single level of hoisting left the update running once per outer
        # iteration. It has to climb the whole nest.
        _, info = _build(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(1000*sizeof(int));\n"
            "  long i, r, s = 0;\n"
            "  for (r = 0; r < 20; r++)\n"
            "    for (i = 0; i < 1000; i++) { a[i] = (int)i; s += a[i]; }\n"
            "  free(a); return (int)(s & 1); }\n")
        self.assertIn("hoisted", info)
        self.assertIn("0 check(s) emitted", info)

    def test_hoisting_preserves_results(self):
        prog = ("#include <stdlib.h>\n#include <stdio.h>\n"
                "int main(void){ int *a = malloc(100*sizeof(int));\n"
                "  int i, s = 0;\n"
                "  for (i = 0; i < 100; i++) { a[i] = i * 2; s += a[i]; }\n"
                "  printf(\"%d\\n\", s); free(a);\n"
                "  return s == 9900 ? 0 : 1; }\n")
        rel, e1 = _build(prog, [])
        chk, e2 = _build(prog, ["--mem-safe"])
        self.assertIsNotNone(rel, e1)
        self.assertIsNotNone(chk, e2)
        a = subprocess.run([rel], capture_output=True, text=True)
        b = subprocess.run([chk], capture_output=True, text=True)
        self.assertEqual(a.stdout, b.stdout)
        self.assertEqual(a.returncode, b.returncode)

    def test_a_loop_that_can_overflow_is_not_hoisted(self):
        rc, err, info = _run(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(4*sizeof(int)); int i;\n"
            "  for (i = 0; i < 6; i++) a[i] = i;\n"
            "  free(a); return 0; }\n")
        self.assertIsNotNone(rc, info)
        self.assertIn("heap buffer overflow", err)
