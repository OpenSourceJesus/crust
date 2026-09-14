"""LeanOS's region list and its model, checked against each other.

`leanos/memmap.py` decides whether a build-time layout is disjoint and
whether an owner may touch an address.  RosettaMath's `memmap_eq.py` is the
same four functions for the proof kernel, with four theorems Lean 4 accepts.
The five legs of `test_elfcheck_model.py`: behaviour over a corpus through
the kernel's evaluator, the same file compiled by ShivyCX, control-flow
shape, export coverage, and Lean.
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
from tests.test_crustos_model import (                   # noqa: E402
    _rosettamath, _in_big_stack, _nat, _skeleton, _body_of)
from tests.test_elfcheck_model import LEAN                # noqa: E402

ROSETTAMATH = _rosettamath()

# The two-thread layout from BAREMETAL_THREADS.md, in small units: kernel
# tables, thread 1 stack, thread 1 slots, thread 2 stack, thread 2 slots.
LAYOUT = ([0, 16, 32, 36, 52], [16, 16, 4, 16, 4], [0, 1, 1, 2, 2])

# (tag, bases, sizes, owners, who, addr, i)
CORPUS = [
    ("layout",      *LAYOUT, 1, 20, 1),
    ("t2_addr",     *LAYOUT, 2, 20, 1),       # thread 2 cannot reach t1's stack
    ("kernel_addr", *LAYOUT, 0, 3, 0),
    ("slots_edge",  *LAYOUT, 1, 35, 2),       # last byte of t1's slots
    ("slots_past",  *LAYOUT, 1, 36, 2),       # first byte of t2's stack
    ("overlap",     [0, 16, 32, 36, 52], [17, 16, 4, 16, 4], LAYOUT[2], 1, 20, 0),
    ("descend",     [16, 0], [4, 4], [1, 2], 1, 17, 0),
    ("touch",       [0, 4], [4, 4], [1, 2], 2, 4, 1),
    ("short_sizes", [0, 16], [16], [1, 2], 1, 3, 0),
    ("short_owners",[0, 16], [16, 16], [1], 2, 20, 1),
    ("empty",       [], [], [], 1, 0, 0),
    ("i_past_end",  [0, 16], [16, 16], [1, 2], 1, 3, 5),
    ("nobody",      LAYOUT[0], LAYOUT[1], LAYOUT[2], 7, 20, 1),
]


def _python(row):
    _, b, s, o, who, addr, i = row
    return (memmap.regions_disjoint(b, s), memmap.contains(b, s, i, addr),
            memmap.owned_by(o, who), memmap.region_of(b, s, o, who, addr))


_MODEL = {}


def _load_model():
    if _MODEL:
        return _MODEL
    sys.path.insert(0, ROSETTAMATH)

    def work():
        import memmap_eq
        import hoare
        import lean4
        env, facts, model = memmap_eq.build()
        return env, facts, model, hoare, lean4

    env, facts, model, hoare, lean4 = _in_big_stack(work)
    _MODEL.update(env=env, facts=facts, model=model, hoare=hoare, lean4=lean4)
    return _MODEL


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestRegionsAgree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loaded = _load_model()
        cls.env, cls.H, cls.L = loaded["env"], loaded["hoare"], loaded["lean4"]
        cls.procs = {k: v.lean_procedure for k, v in loaded["model"].items()}

    def model(self, row):
        H, L, env = self.H, self.L, self.env
        _, b, s, o, who, addr, i = row
        ab, asz, ao = H.array(b), H.array(s), H.array(o)
        terms = [
            H.app(self.procs["regions_disjoint"].fn_term, ab, asz),
            H.app(self.procs["contains"].fn_term, ab, asz, L.numeral(i),
                  L.numeral(addr)),
            H.app(self.procs["owned_by"].fn_term, ao, L.numeral(who)),
            H.app(self.procs["region_of"].fn_term, ab, asz, ao,
                  L.numeral(who), L.numeral(addr)),
        ]
        return _in_big_stack(lambda: tuple(_nat(t, env, L) for t in terms))

    def test_agrees_with_the_model(self):
        for row in CORPUS:
            with self.subTest(case=row[0]):
                self.assertEqual(_python(row), self.model(row),
                                 "memmap and its model disagree on %r" % (row,))

    def test_the_founding_layout_is_disjoint_and_partitioned(self):
        b, s, o = LAYOUT
        self.assertEqual(memmap.regions_disjoint(b, s), 1)
        for addr in range(0, 56):
            owners = [w for w in (0, 1, 2) if memmap.region_of(b, s, o, w, addr)]
            self.assertLessEqual(len(owners), 1,
                                 "address %d reachable by %s" % (addr, owners))


class TestCompiledAgrees(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "leanos", "memmap.py"), d)
        width = max(max(len(r[1]), len(r[2]), len(r[3])) for r in CORPUS)
        c = ["#include <stdio.h>", '#include "memmap.py"',
             "static long ba[%d], sz[%d]; static int ow[%d];" % (width, width, width),
             "static _tlist_long B = { ba, 0, %d }, S = { sz, 0, %d };" % (width, width),
             "static _tlist_int O = { ow, 0, %d };" % width,
             "int main() {"]
        for tag, b, s, o, who, addr, i in CORPUS:
            for k, x in enumerate(b): c.append("  ba[%d] = %dL;" % (k, x))
            for k, x in enumerate(s): c.append("  sz[%d] = %dL;" % (k, x))
            for k, x in enumerate(o): c.append("  ow[%d] = %d;" % (k, x))
            c.append("  B.len = %d; S.len = %d; O.len = %d;" % (len(b), len(s), len(o)))
            c.append('  printf("%s %%d %%d %%d %%d\\n", regions_disjoint(&B, &S),'
                     ' contains(&B, &S, %d, %dL), owned_by(&O, %d),'
                     ' region_of(&B, &S, &O, %d, %dL));' % (tag, i, addr, who, who, addr))
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


MODELLED = {"regions_disjoint", "contains", "owned_by", "region_index",
            "region_of"}


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestModelShape(unittest.TestCase):
    def test_modelled_functions_are_the_same_function_twice(self):
        _load_model()
        import memmap_eq
        for name in sorted(MODELLED):
            with self.subTest(name=name):
                real = _skeleton(_body_of(inspect.getsource(getattr(memmap, name))))
                self.assertEqual(real, _skeleton(_body_of(memmap_eq.SOURCES[name])))


class TestModelCoverage(unittest.TestCase):
    def test_every_exported_function_is_accounted_for(self):
        exported = {n for n, v in vars(memmap).items() if callable(v)
                    and not n.startswith("_")
                    and getattr(v, "__module__", None) == memmap.__name__}
        self.assertEqual(exported, MODELLED)


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
@unittest.skipUnless(LEAN, "no lean on PATH; run 'make install_lean'")
class TestLeanAccepts(unittest.TestCase):
    def test_all_four_with_no_axioms(self):
        loaded = _load_model()
        import memmap_eq
        src = _in_big_stack(lambda: memmap_eq.lean_source(loaded["env"]))
        path = os.path.join(tempfile.mkdtemp(), "MemMap.lean")
        with open(path, "w") as fh:
            fh.write(src)
        run = subprocess.run([LEAN, path], capture_output=True, text=True, timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for name in memmap_eq.THEOREMS:
            self.assertIn("'RM.%s' does not depend on any axioms" % name, run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
