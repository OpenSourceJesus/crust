"""LeanOS's bump allocator and its model, checked against each other.

`leanos/alloc.py` moves a counter up a region the thread owns.
RosettaMath's `alloc_eq.py` is the same functions for the proof kernel, with
`bump_bounded`, `bump_monotone` and `slot_in_bounds` accepted by Lean 4.  The legs of
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
import alloc                                             # noqa: E402
# `alloc.py` calls `contains` by textual inclusion after `memmap.py`; run as
# Python it needs the same name in scope.
alloc.contains = memmap.contains
from tests.test_crustos_model import (                   # noqa: E402
    _rosettamath, _in_big_stack, _nat, _skeleton, _body_of)
from tests.test_elfcheck_model import LEAN                # noqa: E402
from tests.test_memmap_model import LAYOUT                # noqa: E402

ROSETTAMATH = _rosettamath()
B, S, O = LAYOUT       # kernel [0,16), t0 stack [16,32), t0 slots [32,36),
                       # t1 stack [36,52), t1 slots [52,56)

# (tag, tid, heap, used, n).  Thread 0's heap is region 2 (4 bytes).
CORPUS = [
    ("fits",       0, 2, 0, 3),
    ("fills",      0, 2, 3, 1),       # used + n == size: accepted
    ("full",       0, 2, 4, 1),       # refused
    ("over",       0, 2, 2, 5),
    ("zero",       0, 2, 2, 0),
    ("not_mine",   1, 2, 0, 1),       # region 2 is thread 0's
    ("kernel",     0, 0, 0, 1),       # region 0 is the kernel's
    ("bad_heap",   0, 9, 0, 1),       # no such region
    ("t1_heap",    1, 4, 1, 3),       # thread 1's slots, region 4
    ("at_end",     0, 2, 4, 0),       # slot_ok at used == size: 0
]


def _python(row):
    _, tid, heap, used, n = row
    return (alloc.bump(B, S, O, tid, heap, used, n),
            alloc.slot_addr(B, heap, used) if heap < len(B) else 0,
            alloc.slot_ok(B, S, heap, used) if heap < len(B) else 0)


_MODEL = {}


def _load_model():
    if _MODEL:
        return _MODEL
    sys.path.insert(0, ROSETTAMATH)

    def work():
        import alloc_eq
        import hoare
        import lean4
        env, facts, model = alloc_eq.build()
        return env, facts, model, hoare, lean4

    env, facts, model, hoare, lean4 = _in_big_stack(work)
    _MODEL.update(env=env, facts=facts, model=model, hoare=hoare, lean4=lean4)
    return _MODEL


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestAllocAgrees(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loaded = _load_model()
        cls.env, cls.H, cls.L = loaded["env"], loaded["hoare"], loaded["lean4"]
        cls.procs = {k: v.lean_procedure for k, v in loaded["model"].items()}

    def model(self, row):
        H, L, env = self.H, self.L, self.env
        _, tid, heap, used, n = row
        ab, asz, ao = H.array(B), H.array(S), H.array(O)
        num = L.numeral
        terms = [
            H.app(self.procs["bump"].fn_term, ab, asz, ao, num(tid), num(heap),
                  num(used), num(n)),
            H.app(self.procs["slot_addr"].fn_term, ab, num(heap), num(used)),
            H.app(self.procs["slot_ok"].fn_term, ab, asz, num(heap), num(used)),
        ]
        return _in_big_stack(lambda: tuple(_nat(t, env, L) for t in terms))

    def test_agrees_with_the_model(self):
        for row in CORPUS:
            with self.subTest(case=row[0]):
                if row[2] >= len(B):
                    continue      # the model's nth is 0 past the end; C's is not defined
                self.assertEqual(_python(row), self.model(row),
                                 "alloc and its model disagree on %r" % (row,))

    def test_bump_never_exceeds_the_region(self):
        # the theorems, checked by hand: for every used <= size and every n,
        # used <= bump <= size, and every slot below size is in the region
        for tid, heap in ((0, 2), (1, 4)):
            size = S[heap]
            for used in range(0, size + 1):
                for n in range(0, 10):
                    after = alloc.bump(B, S, O, tid, heap, used, n)
                    self.assertTrue(used <= after <= size)
                if used < size:
                    self.assertEqual(alloc.slot_ok(B, S, heap, used), 1)


class TestCompiledAgrees(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp()
        for name in ("memmap.py", "alloc.py"):
            shutil.copy(os.path.join(ROOT, "leanos", name), d)
        c = ["#include <stdio.h>", '#include "memmap.py"', '#include "alloc.py"',
             "static long ba[5] = {%s}, sz[5] = {%s}; static int ow[5] = {%s};"
             % (",".join(map(str, B)), ",".join(map(str, S)), ",".join(map(str, O))),
             "static _tlist_long LB = { ba, 5, 5 }, LS = { sz, 5, 5 };",
             "static _tlist_int LO = { ow, 5, 5 };",
             "int main() {"]
        for tag, tid, heap, used, n in CORPUS:
            if heap < len(B):
                c.append('  printf("%s %%ld %%ld %%d\\n", bump(&LB,&LS,&LO,%d,%d,%dL,%dL),'
                         ' slot_addr(&LB,%d,%dL), slot_ok(&LB,&LS,%d,%dL));'
                         % (tag, tid, heap, used, n, heap, used, heap, used))
            else:
                c.append('  printf("%s %%ld 0 0\\n", bump(&LB,&LS,&LO,%d,%d,%dL,%dL));'
                         % (tag, tid, heap, used, n))
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


MODELLED = {"bump", "slot_addr", "slot_ok"}


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestModelShape(unittest.TestCase):
    def test_modelled_functions_are_the_same_function_twice(self):
        _load_model()
        import alloc_eq
        for name in sorted(MODELLED):
            with self.subTest(name=name):
                real = _skeleton(_body_of(inspect.getsource(getattr(alloc, name))))
                self.assertEqual(real, _skeleton(_body_of(alloc_eq.SOURCES[name])))


class TestModelCoverage(unittest.TestCase):
    def test_every_exported_function_is_accounted_for(self):
        exported = {n for n, v in vars(alloc).items() if callable(v)
                    and not n.startswith("_")
                    and getattr(v, "__module__", None) == alloc.__name__}
        self.assertEqual(exported, MODELLED)


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
@unittest.skipUnless(LEAN, "no lean on PATH; run 'make install_lean'")
class TestLeanAccepts(unittest.TestCase):
    def test_all_three_with_no_axioms(self):
        loaded = _load_model()
        import alloc_eq
        src = _in_big_stack(lambda: alloc_eq.lean_source(loaded["env"]))
        path = os.path.join(tempfile.mkdtemp(), "Alloc.lean")
        with open(path, "w") as fh:
            fh.write(src)
        run = subprocess.run([LEAN, path], capture_output=True, text=True, timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for name in alloc_eq.THEOREMS:
            self.assertIn("'RM.%s' does not depend on any axioms" % name, run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
