"""LeanOS threads and their model, checked against each other.

`leanos/threads.py` is a thread as a record over the region list.
RosettaMath's `threads_eq.py` is the same functions for the proof kernel,
with `push_keeps_sp_ok` and `sp_no_cross` accepted by Lean 4.  The legs of
`test_memmap_model.py`: behaviour through the kernel's evaluator, the file
compiled by ShivyCX together with `memmap.py` in one unit, shape, coverage,
and Lean.
"""
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "leanos"))

import memmap                                            # noqa: E402
import threads                                           # noqa: E402
# `threads.py` calls `region_of` the way `kernel.c` reaches `schemes.py`:
# by textual inclusion after `memmap.py`.  Run as Python it needs the same
# name in scope, so the test supplies it the way the include order would.
threads.region_of = memmap.region_of
from tests.test_crustos_model import (                   # noqa: E402
    _rosettamath, _in_big_stack, _nat, _skeleton, _body_of)
from tests.test_elfcheck_model import LEAN                # noqa: E402
from tests.test_memmap_model import LAYOUT                # noqa: E402

ROSETTAMATH = _rosettamath()
B, S, O = LAYOUT       # kernel [0,16), t0 stack [16,32), t0 slots [32,36),
                       # t1 stack [36,52), t1 slots [52,56)

# (tag, sps, tid, sp, n)
CORPUS = [
    ("home",        [30, 50], 0, 30, 8),      # push stays in t0's stack
    ("to_base",     [30, 50], 0, 30, 14),     # lands exactly on base 16: ok
    ("past_base",   [30, 50], 0, 30, 15),     # 15 is the kernel's: refused
    ("too_big",     [30, 50], 0, 30, 40),     # n > sp: refused
    ("zero",        [30, 50], 0, 30, 0),
    ("t1_home",     [30, 50], 1, 50, 10),
    ("t1_into_t0",  [30, 50], 1, 40, 5),      # 35 is t0's slots: refused
    ("t1_in_t0",    [30, 33], 1, 33, 0),      # t1 pointing into t0's slots
    ("t0_in_kernel",[8, 50],  0, 8, 0),
    ("both_bad",    [8, 33],  0, 8, 1),
    ("nobody",      [30, 50], 7, 30, 0),      # thread 7 owns nothing
]


def _python(row):
    _, sps, tid, sp, n = row
    return (threads.sp_ok(B, S, O, tid, sp), threads.all_sps_ok(B, S, O, sps),
            threads.sp_after_push(B, S, O, tid, sp, n), threads.thread_owner(tid))


_MODEL = {}


def _load_model():
    if _MODEL:
        return _MODEL
    sys.path.insert(0, ROSETTAMATH)

    def work():
        import threads_eq
        import hoare
        import lean4
        env, facts, model = threads_eq.build()
        return env, facts, model, hoare, lean4

    env, facts, model, hoare, lean4 = _in_big_stack(work)
    _MODEL.update(env=env, facts=facts, model=model, hoare=hoare, lean4=lean4)
    return _MODEL


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestThreadsAgree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loaded = _load_model()
        cls.env, cls.H, cls.L = loaded["env"], loaded["hoare"], loaded["lean4"]
        cls.procs = {k: v.lean_procedure for k, v in loaded["model"].items()}

    def model(self, row):
        H, L, env = self.H, self.L, self.env
        _, sps, tid, sp, n = row
        ab, asz, ao, asp = H.array(B), H.array(S), H.array(O), H.array(sps)
        terms = [
            H.app(self.procs["sp_ok"].fn_term, ab, asz, ao, L.numeral(tid), L.numeral(sp)),
            H.app(self.procs["all_sps_ok"].fn_term, ab, asz, ao, asp),
            H.app(self.procs["sp_after_push"].fn_term, ab, asz, ao, L.numeral(tid),
                  L.numeral(sp), L.numeral(n)),
            H.app(self.procs["thread_owner"].fn_term, L.numeral(tid)),
        ]
        return _in_big_stack(lambda: tuple(_nat(t, env, L) for t in terms))

    def test_agrees_with_the_model(self):
        for row in CORPUS:
            with self.subTest(case=row[0]):
                self.assertEqual(_python(row), self.model(row),
                                 "threads and its model disagree on %r" % (row,))

    def test_push_never_leaves_the_region(self):
        # the theorem, checked by hand over the layout: every sp in a thread's
        # region, pushed by any n, is still in it
        for tid, lo, hi in ((0, 16, 32), (1, 36, 52)):
            for sp in range(lo, hi):
                for n in range(0, 60):
                    after = threads.sp_after_push(B, S, O, tid, sp, n)
                    self.assertEqual(threads.sp_ok(B, S, O, tid, after), 1)


class TestCompiledAgrees(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp()
        for name in ("memmap.py", "threads.py"):
            shutil.copy(os.path.join(ROOT, "leanos", name), d)
        width = max(len(r[1]) for r in CORPUS)
        c = ["#include <stdio.h>", '#include "memmap.py"', '#include "threads.py"',
             "static long ba[5] = {%s}, sz[5] = {%s}; static int ow[5] = {%s};"
             % (",".join(map(str, B)), ",".join(map(str, S)), ",".join(map(str, O))),
             "static long sp[%d];" % width,
             "static _tlist_long LB = { ba, 5, 5 }, LS = { sz, 5, 5 }, LSP = { sp, 0, %d };" % width,
             "static _tlist_int LO = { ow, 5, 5 };",
             "int main() {"]
        for tag, sps, tid, sp_, n in CORPUS:
            for k, x in enumerate(sps):
                c.append("  sp[%d] = %dL;" % (k, x))
            c.append("  LSP.len = %d;" % len(sps))
            c.append('  printf("%s %%d %%d %%ld %%d\\n", sp_ok(&LB,&LS,&LO,%d,%dL),'
                     ' all_sps_ok(&LB,&LS,&LO,&LSP), sp_after_push(&LB,&LS,&LO,%d,%dL,%dL),'
                     ' thread_owner(%d));' % (tag, tid, sp_, tid, sp_, n, tid))
        c.append("  return 0; }")
        with open(os.path.join(d, "t.c"), "w") as fh:
            fh.write("\n".join(c))
        proc = subprocess.run(
            [sys.executable, "-m", "shivyc.main", "--no-cache",
             os.path.join(d, "t.c"), "-o", os.path.join(d, "t")],
            capture_output=True, text=True, cwd=ROOT)
        assert proc.returncode == 0, proc.stderr[-1200:]
        run = subprocess.run([os.path.join(d, "t")], capture_output=True, text=True)
        cls.compiled = {ln.split()[0]: tuple(int(x) for x in ln.split()[1:])
                        for ln in run.stdout.splitlines()}

    def test_compiled_agrees_with_python(self):
        for row in CORPUS:
            with self.subTest(case=row[0]):
                self.assertEqual(_python(row), self.compiled[row[0]])


MODELLED = {"thread_owner", "sp_ok", "all_sps_ok", "sp_after_push"}


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestModelShape(unittest.TestCase):
    def test_modelled_functions_are_the_same_function_twice(self):
        _load_model()
        import threads_eq
        for name in sorted(MODELLED):
            with self.subTest(name=name):
                real = _skeleton(_body_of(inspect.getsource(getattr(threads, name))))
                self.assertEqual(real, _skeleton(_body_of(threads_eq.SOURCES[name])))


class TestModelCoverage(unittest.TestCase):
    def test_every_exported_function_is_accounted_for(self):
        exported = {n for n, v in vars(threads).items() if callable(v)
                    and not n.startswith("_")
                    and getattr(v, "__module__", None) == threads.__name__}
        self.assertEqual(exported, MODELLED)


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
@unittest.skipUnless(LEAN, "no lean on PATH; run 'make install_lean'")
class TestLeanAccepts(unittest.TestCase):
    def test_both_with_no_axioms(self):
        loaded = _load_model()
        import threads_eq
        src = _in_big_stack(lambda: threads_eq.lean_source(loaded["env"]))
        path = os.path.join(tempfile.mkdtemp(), "Threads.lean")
        with open(path, "w") as fh:
            fh.write(src)
        run = subprocess.run([LEAN, path], capture_output=True, text=True, timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for name in threads_eq.THEOREMS:
            self.assertIn("'RM.%s' does not depend on any axioms" % name, run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
