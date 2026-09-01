"""The C tier of --mem-safe: checks inserted by the compiler's IL pass, with no
macros anywhere in the source.

The macro tier (tests/test_mem_safe.py) only guards accesses someone wrote
CRUST_MS_* around, which is right for generated C and useless for a directory
of hand-written .c files. This tier instruments the IL instead, so plain C gets
the same checks untouched.

Two properties matter more than raw detection and are tested hardest:

* **No false positives.** A checked build of a correct program must be silent
  and must produce byte-identical output. A tool that cries wolf on `strcpy`
  buffers gets turned off, and then it catches nothing at all.
* **No miscompilation.** Instrumented and release builds must agree on results.
"""
import os
import subprocess
import tempfile
import unittest


def _build(src, flags):
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
    """Build with --mem-safe and run; return (exit status, stderr, stdout)."""
    out, info = _build(src, ["--mem-safe"])
    if out is None:
        return None, info, ""
    p = subprocess.run([out], capture_output=True, text=True)
    return p.returncode, p.stderr, p.stdout


class TestILDetection(unittest.TestCase):
    """Every class, from plain C with no annotation of any kind."""

    def _assert_reports(self, src, phrase):
        rc, err, _ = _run(src)
        self.assertIsNotNone(rc, err)
        self.assertIn(phrase, err)
        self.assertEqual(rc, 1)

    def test_heap_overflow(self):
        self._assert_reports(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(4*sizeof(int));\n"
            "  a[0]=1; a[4]=99; free(a); return 0; }\n",
            "heap buffer overflow")

    def test_use_after_free(self):
        self._assert_reports(
            "#include <stdlib.h>\n"
            "int main(void){ int *p = malloc(4); *p = 1; free(p);\n"
            "  return *p; }\n",
            "use after free")

    def test_double_free(self):
        self._assert_reports(
            "#include <stdlib.h>\n"
            "int main(void){ char *s = malloc(8); free(s); free(s);\n"
            "  return 0; }\n",
            "double free")

    def test_uninitialized_read(self):
        self._assert_reports(
            "#include <stdlib.h>\n"
            "int main(void){ int *p = malloc(8); p[0]=1; return p[1]; }\n",
            "uninitialized")

    def test_free_of_interior_pointer(self):
        self._assert_reports(
            "#include <stdlib.h>\n"
            "int main(void){ char *s = malloc(16); free(s + 4); return 0; }\n",
            "interior pointer")

    def test_report_names_the_access_line(self):
        rc, err, _ = _run(
            "#include <stdlib.h>\n"             # 1
            "int main(void){\n"                 # 2
            "  int *a = malloc(8);\n"           # 3
            "  a[7] = 1;\n"                     # 4  out of bounds
            "  free(a); return 0; }\n")
        self.assertIsNotNone(rc, err)
        self.assertIn("t.c:4", err)
        self.assertIn("t.c:3", err)             # the allocation


class TestNoFalsePositives(unittest.TestCase):
    def test_correct_program_is_clean(self):
        rc, err, out = _run(
            "#include <stdlib.h>\n"
            "int main(void){ int *p = malloc(8); p[0]=1; p[1]=2;\n"
            "  int v = p[0]+p[1]; free(p); return v-3; }\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("clean", err)

    def test_buffer_filled_by_uninstrumented_libc(self):
        # strcpy writes bytes the shadow never sees. Without the escape hook
        # every later read of them is reported as uninitialized -- the single
        # most common false positive this tier could produce, and the one that
        # would get the flag switched off.
        rc, err, _ = _run(
            "#include <stdlib.h>\n#include <string.h>\n"
            "int main(void){ char *b = malloc(64); strcpy(b, \"hi\");\n"
            "  int n = (b[0] == 'h'); free(b); return n ? 0 : 1; }\n")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("uninitialized", err)

    def test_linked_structure_walk_is_clean(self):
        rc, err, _ = _run(
            "#include <stdlib.h>\n"
            "struct n { int k; struct n *next; };\n"
            "int main(void){ struct n *h = 0; int i, s = 0;\n"
            "  for (i = 0; i < 20; i++) {\n"
            "    struct n *x = malloc(sizeof(struct n));\n"
            "    x->k = i; x->next = h; h = x; }\n"
            "  while (h) { struct n *x = h->next; s += h->k; free(h); h = x; }\n"
            "  return s == 190 ? 0 : 1; }\n")
        self.assertEqual(rc, 0, err)


class TestSemanticsPreserved(unittest.TestCase):
    PROG = ("#include <stdlib.h>\n#include <stdio.h>\n"
            "int main(void){ int *a = malloc(50*sizeof(int));\n"
            "  int i, s = 0;\n"
            "  for (i = 0; i < 50; i++) a[i] = i * 3;\n"
            "  for (i = 0; i < 50; i++) s += a[i];\n"
            "  printf(\"%d\\n\", s); free(a); return s == 3675 ? 0 : 1; }\n")

    def test_instrumented_build_computes_the_same_answer(self):
        rel, err1 = _build(self.PROG, [])
        chk, err2 = _build(self.PROG, ["--mem-safe"])
        self.assertIsNotNone(rel, err1)
        self.assertIsNotNone(chk, err2)
        a = subprocess.run([rel], capture_output=True, text=True)
        b = subprocess.run([chk], capture_output=True, text=True)
        self.assertEqual(a.stdout, b.stdout)
        self.assertEqual(a.returncode, b.returncode)


class TestTierInteraction(unittest.TestCase):
    def test_macro_instrumented_source_is_not_double_checked(self):
        # A unit already carrying macro checks was instrumented by the C++
        # tier. Re-checking here would run two checks per dereference and
        # report each bug twice.
        src = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "examples", "memory",
            "memsafe_runtime.c")
        with open(src) as f:
            fixture = f.read()
        out, info = _build(fixture, ["--mem-safe"])
        self.assertIsNotNone(out, info)
        self.assertIn("0 check(s) emitted", info)
        p = subprocess.run([out], capture_output=True, text=True)
        # The fixture has exactly five distinct bugs; a double-instrumented
        # build reported eight.
        self.assertIn("5 errors", p.stderr)

    def test_runtime_itself_is_not_instrumented(self):
        # Instrumenting the checker makes crust_ms_malloc call itself, and the
        # first allocation recurses until the stack is gone.
        out, info = _build(
            "#include <stdlib.h>\n"
            "int main(void){ int *p = malloc(4); *p = 1; free(p);\n"
            "  return 0; }\n", ["--mem-safe"])
        self.assertIsNotNone(out, info)
        p = subprocess.run([out], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_cpp_level_does_not_run_the_il_pass(self):
        # --mem-safe=cpp is the C++ tier: it defines CRUST_MEM_SAFE for the
        # macros but must leave hand-written C alone at full speed.
        out, info = _build(
            "#include <stdlib.h>\n"
            "int main(void){ int *a = malloc(4); a[4] = 1; free(a);\n"
            "  return 0; }\n", ["--mem-safe=cpp"])
        self.assertIsNotNone(out, info)
        self.assertNotIn("check(s) emitted", info)


if __name__ == "__main__":
    unittest.main()


class TestStackObjects(unittest.TestCase):
    """Frame objects. Local arrays lower to ReadRel/SetRel, not ReadAt/SetAt,
    so they went unchecked entirely until those forms were instrumented and
    their base objects registered."""

    def test_local_array_overflow_is_caught(self):
        rc, err, _ = _run(
            "int main(void){ int buf[8]; int i;\n"
            "  for (i = 0; i < 8; i++) buf[i] = i;\n"
            "  buf[9] = 99;\n"
            "  return buf[0]; }\n")
        self.assertIsNotNone(rc, err)
        self.assertIn("stack buffer overflow", err)
        self.assertIn("buf", err)          # the variable is named
        self.assertEqual(rc, 1)

    def test_use_after_scope_exit(self):
        rc, err, _ = _run(
            "static int *escape(void){ int local = 42; return &local; }\n"
            "int main(void){ int *p = escape(); return *p; }\n")
        self.assertIsNotNone(rc, err)
        self.assertIn("scope", err)

    def test_uninitialized_local_array(self):
        rc, err, _ = _run(
            "int main(void){ int b[4]; int i, s = 0;\n"
            "  for (i = 0; i < 4; i++) s += b[i];\n"
            "  return s; }\n")
        self.assertIsNotNone(rc, err)
        self.assertIn("uninitialized", err)

    def test_by_value_parameter_is_not_reported_uninitialized(self):
        # A struct parameter arrives through LoadArg/LoadStructArg, which is
        # not a memory access and is never instrumented. Registering it as
        # undefined reported a false uninitialized read on its first use.
        rc, err, _ = _run(
            "struct p { int a; int b; };\n"
            "static int use(struct p v){ return v.a + v.b; }\n"
            "int main(void){ struct p x; x.a = 1; x.b = 2;\n"
            "  return use(x) == 3 ? 0 : 1; }\n")
        self.assertIsNotNone(rc, err)
        self.assertNotIn("uninitialized", err)
        self.assertEqual(rc, 0, err)

    def test_struct_by_value_function_is_not_miscompiled(self):
        # Registration is emitted after the prologue, and a function taking a
        # struct by value begins with LoadStructArg rather than LoadArg. A
        # scan that knew only LoadArg inserted the call above the argument
        # load, clobbering the argument registers.
        rc, err, out = _run(
            "#include <stdio.h>\n"
            "struct s { int a; int b; int c; };\n"
            "struct s make(struct s in){ struct s o;\n"
            "  o.a = in.a + 1; o.b = in.b + 1; o.c = in.c + 1; return o; }\n"
            "int main(void){ struct s x; x.a=1; x.b=2; x.c=3;\n"
            "  struct s y = make(x);\n"
            "  printf(\"%d\\n\", y.a + y.b + y.c);\n"
            "  return y.a + y.b + y.c == 9 ? 0 : 1; }\n")
        self.assertIsNotNone(rc, err)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "9")

    def test_string_literal_is_not_registered_as_a_frame_object(self):
        # String literals live in .rodata and are never declared, so they are
        # absent from the storage map. Registering one put a bogus region in
        # the table and the underflow search blamed it for reads of the static
        # data beside it.
        rc, err, _ = _run(
            "#include <stdio.h>\n"
            "int main(void){ const char *m = \"hello\";\n"
            "  printf(\"%s\\n\", m); return 0; }\n")
        self.assertIsNotNone(rc, err)
        self.assertEqual(rc, 0, err)
