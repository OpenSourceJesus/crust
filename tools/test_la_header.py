"""The fixed-size linear algebra header, on all three paths it has to work on.

`examples/eigen/la.h` is ordinary C++. It contains no `assert`, no
intrinsic, no lane width, and no mention of SIMD -- the contracts that turn
ShivyCX's vectorizer on are *inferred* from the struct each operator takes,
under `--contracts`. So the header has three consumers and they must agree:

    g++ on the .cpp                     the reference
    cpprust -> gcc                      the ordinary Crust build
    cpprust --contracts -> ShivyCX      the self-backend, vectorized

Agreement on the result is the correctness claim. The `simd-contracts`
reports and the emitted packed instructions are the performance claim, and
they are asserted separately -- a kernel that stopped vectorizing would
still return 51, so the number alone would not notice.

Why the header carries no contracts: a clause is a ShivyCX extension and
passes through cpprust unconditionally, so a header that wrote one would
not be C++ at all and g++ would reject it outright. Inferring them instead
is what keeps the first two columns above possible.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.append(os.path.split(__file__)[0])
import cpprust

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EX = os.path.join(_ROOT, "examples", "eigen")
_DEMO = os.path.join(_EX, "demo.cpp")

#: 7 + (-1) + 5 + 32 + 8, computed by hand from the demo's inputs rather
#: than from any of the three builds -- otherwise the three could agree on
#: the same wrong answer.
_EXPECT = 51


def _have(tool):
    try:
        subprocess.check_output([tool, "--version"], stderr=subprocess.STDOUT)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _lower(contracts):
    with open(_DEMO) as f:
        src = f.read()
    return cpprust.translate(src, path=_DEMO, basedir=_EX,
                             contracts=contracts)


@unittest.skipUnless(os.path.exists(_DEMO), "examples/eigen not present")
class TestLaHeader(unittest.TestCase):

    def _build_c(self, text, cc):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(text)
        exe = os.path.join(d, "t")
        subprocess.check_output([cc, "-std=c11", "-w", "-o", exe, c],
                                stderr=subprocess.STDOUT)
        return exe

    def _shivyc(self, text, asm=False):
        """Build with ShivyCX; returns (output path, the build log).

        `asm=True` stops at the assembly, which is where the vectorization
        is visible -- the reports alone say a kernel was proven, not that
        packed instructions came out of it.
        """
        d = tempfile.mkdtemp()
        c = os.path.join(d, "t.c")
        with open(c, "w") as f:
            f.write(text)
        target = os.path.join(d, "t.s" if asm else "t")
        out = subprocess.run(
            ["python3", "-m", "shivyc.main", c]
            + (["-S"] if asm else []) + ["-o", target],
            cwd=_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(out.returncode, 0, out.stdout.decode()[-2000:])
        return target, out.stdout.decode()

    # -- the header is plain C++ ------------------------------------------

    def test_the_header_writes_no_contracts(self):
        with open(os.path.join(_EX, "la.h")) as f:
            body = "".join(l for l in f if not l.lstrip().startswith("*"))
        self.assertNotIn("assert", body)

    def test_the_default_lowering_emits_none_either(self):
        """Without `--contracts` the C is ordinary C, or gcc could not
        build it: a clause is not something a C compiler can parse."""
        self.assertNotIn("assert", _lower(contracts=False))

    # -- the three paths agree --------------------------------------------

    @unittest.skipUnless(_have("g++"), "g++ not available")
    def test_gpp_reference(self):
        d = tempfile.mkdtemp()
        exe = os.path.join(d, "r")
        subprocess.check_output(
            ["g++", "-std=c++11", "-w", "-I", _EX, "-o", exe, _DEMO],
            stderr=subprocess.STDOUT)
        self.assertEqual(subprocess.call([exe]), _EXPECT)

    @unittest.skipUnless(_have("gcc"), "gcc not available")
    def test_cpprust_then_gcc(self):
        exe = self._build_c(_lower(contracts=False), "gcc")
        self.assertEqual(subprocess.call([exe]), _EXPECT)

    def test_cpprust_contracts_then_shivycx(self):
        exe, _ = self._shivyc(_lower(contracts=True))
        self.assertEqual(subprocess.call([exe]), _EXPECT)

    # -- and the vectorized path is actually vectorized -------------------

    def test_the_operators_are_proven(self):
        """A kernel that quietly stopped vectorizing would still return 51,
        so the reports are asserted rather than inferred from the answer."""
        _exe, log = self._shivyc(_lower(contracts=True))
        for fn in ("Vec_float_16__binadd", "Vec_float_16__binsub",
                   "Vec_float_16__binmul", "Vec_float_16_scaled",
                   "Vec_float_16_dot"):
            self.assertIn("'%s': contracts proven" % fn, log)

    def test_the_packed_instructions_are_emitted(self):
        s, _log = self._shivyc(_lower(contracts=True), asm=True)
        with open(s) as f:
            asm = f.read()

        def body(fn):
            return asm.split("\n%s:" % fn)[1].split("\n\tret")[0]

        self.assertIn("addps", body("Vec_float_16__binadd"))
        self.assertIn("subps", body("Vec_float_16__binsub"))
        self.assertIn("mulps", body("Vec_float_16__binmul"))
        # A broadcast splats the scalar across the lanes once.
        self.assertIn("shufps", body("Vec_float_16_scaled"))
        # A dot is a reduction: multiply, accumulate, horizontal sum.
        dot = body("Vec_float_16_dot")
        self.assertIn("mulps", dot)
        self.assertIn("addps", dot)
        # No scalar remainder anywhere -- that is what the contract buys.
        for fn in ("Vec_float_16__binadd", "Vec_float_16__binsub",
                   "Vec_float_16__binmul"):
            self.assertNotIn("addss", body(fn))
            self.assertNotIn("subss", body(fn))

    def test_the_operator_writes_straight_into_the_return_slot(self):
        """The whole of `operator+` is the loop: the local it names, that
        local's zeroing constructor, and the copy out are all elided, so
        there is no call left in it."""
        s, _log = self._shivyc(_lower(contracts=True), asm=True)
        with open(s) as f:
            body = f.read().split("\nVec_float_16__binadd:")[1] \
                           .split("\n\tret")[0]
        self.assertNotIn("call", body)
        self.assertIn("[rdi + rax]", body)      # sret destination
        self.assertIn("[rsi + rax]", body)      # this->d
        self.assertIn("[rdx + rax]", body)      # o->d


if __name__ == "__main__":
    unittest.main()
