"""End-to-end tests for contract-driven SIMD and metamorphic returns.

Each test drives the full pipeline (extension pre-pass, IL, the new passes,
assembly, and linking), runs the binary, and checks both the result and the
emitted assembly.
"""

import os
import subprocess
import tempfile
import unittest

import shivyc.main
from shivyc.errors import error_collector


class _Args:
    show_reg_alloc_perf = False
    variables_on_stack = False
    simd_pack_globals = False
    stackless_calls = False
    metamorphic = False
    opt_level = 0

    def __init__(self, files, output_name):
        self.files = files
        self.output_name = output_name


def _run(source, **flags):
    """Compile `source` with the given flags; return (exit_code, asm_text)."""
    workdir = tempfile.mkdtemp()
    c_path = os.path.join(workdir, "prog.c")
    out_path = os.path.join(workdir, "prog")
    with open(c_path, "w") as f:
        f.write(source)

    args = _Args([c_path], [out_path])
    for k, v in flags.items():
        setattr(args, k, v)

    shivyc.main.get_arguments = lambda: args
    error_collector.show = lambda: True
    error_collector.clear()
    rc = shivyc.main.main()
    assert rc == 0, "compilation failed"

    with open(os.path.join(workdir, "prog.s")) as f:
        asm = f.read()
    return subprocess.run([out_path]).returncode, asm


SIMD_SRC = """
    extern void *malloc(unsigned long size);
    int calc_sum(int *ptr, unsigned int len)
    assert len(ptr) >= 64
    assert not len(ptr) % 4
    {
      int v = 0; unsigned int i = 0;
      for (i = 0; i < len; i = i + 1) { v = v + ptr[i]; }
      return v;
    }
    int run() {
      int *ptr = malloc(NNN * sizeof(int));
      unsigned int i = 0;
      for (i = 0; i < NNN; i = i + 1) { ptr[i] = 2; }
      return calc_sum(ptr, NNN);
    }
    int main() { return run(); }
"""


def _simd_src(n):
    return SIMD_SRC.replace("NNN", str(n))


class TestSimdContracts(unittest.TestCase):
    """Contracts proven from the call graph license a fallback-free SIMD loop."""

    def test_proven_emits_sse2_and_is_correct(self):
        rc, asm = _run(_simd_src(64))
        self.assertEqual(rc, 128)              # 64 elements * 2
        calc = asm.split("calc_sum:")[1].split("run:")[0]
        self.assertIn("paddd", calc)           # vectorized
        self.assertNotIn("READAT", calc)       # no scalar element loop

    def test_unprovable_keeps_scalar_and_is_correct(self):
        # 70 is not a multiple of 4, so alignment cannot be proven.
        rc, asm = _run(_simd_src(70))
        self.assertEqual(rc, 140)              # 70 elements * 2, scalar
        calc = asm.split("calc_sum:")[1].split("run:")[0]
        self.assertNotIn("paddd", calc)        # stayed scalar (still correct)


class TestMetamorphic(unittest.TestCase):
    """Metamorphic returns route through a self-modified slot, not the stack."""

    SRC = """
        int helper(int x) __metamorphic__ { return x + 5; }
        int main() {
          int a = helper(10);
          int b = helper(a);
          return a + b;            /* 15 + 20 = 35 */
        }
    """

    def test_correct_result(self):
        rc, _ = _run(self.SRC, metamorphic=True)
        self.assertEqual(rc, 35)

    def test_uses_slot_not_stack(self):
        _, asm = _run(self.SRC, metamorphic=True)
        helper = asm.split("helper:")[1].split("main:")[0]
        self.assertIn("jmp QWORD PTR [rip + helper__metaret]", helper)
        self.assertNotIn("\tret\n", helper)    # no stack return instruction
        # The return slot lives in writable *data*, off the code page -- not in
        # a writable+executable section next to the function. The old .mtext
        # layout put the slot in the code's own cache line, so the caller's
        # store forced a self-modifying-code machine clear on every call.
        self.assertNotIn(".mtext", asm)
        data = asm.split(".section .data")[1].split(".section .text")[0]
        self.assertIn("helper__metaret:", data)   # slot is in .data
        # Caller patches the slot and jumps instead of calling.
        self.assertIn("mov QWORD PTR [rip + helper__metaret]", asm)

    def test_no_flag_is_normal(self):
        rc, asm = _run(self.SRC, metamorphic=False)
        self.assertEqual(rc, 35)
        self.assertNotIn(".mtext", asm)
        self.assertNotIn("metaret", asm)

    def test_recursion_refused(self):
        # A re-entrant metamorphic function would corrupt its single slot, so
        # the compiler must refuse rather than emit crashing code.
        src = ("int fact(int n) __metamorphic__ {\n"
               "  if (n <= 1) { return 1; }\n"
               "  return n * fact(n - 1);\n"
               "}\n"
               "int main() { return fact(5); }\n")
        workdir = tempfile.mkdtemp()
        c_path = os.path.join(workdir, "p.c")
        with open(c_path, "w") as f:
            f.write(src)
        args = _Args([c_path], [os.path.join(workdir, "p")])
        args.metamorphic = True
        shivyc.main.get_arguments = lambda: args
        error_collector.show = lambda: True
        error_collector.clear()
        self.assertEqual(shivyc.main.main(), 1)   # refused


class TestPerFunctionStackless(unittest.TestCase):
    """The __stackless__ specifier opts a single function in without the flag."""

    SRC = """
        int accum;
        void sum() { accum = accum + 1; }
        void foo() __stackless__ { sum(); }
        void bar() { foo(); sum(); }
        int main() { accum = 0; foo(); bar(); return accum; }
    """

    def test_marked_function_optimized_others_not(self):
        rc, asm = _run(self.SRC)
        self.assertEqual(rc, 3)
        foo = asm.split("foo:")[1].split("bar:")[0]
        bar = asm.split("bar:")[1].split("main:")[0]
        self.assertIn("jmp sum", foo)          # foo optimized
        self.assertNotIn("push rbp", foo)      # frameless
        self.assertIn("push rbp", bar)         # bar untouched


if __name__ == "__main__":
    unittest.main()
