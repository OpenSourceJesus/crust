"""LeanOS's loader and its model, checked against each other.

`leanos/loader.py` admits an ELF by `accept_image` and then `regions_disjoint`
on the region list with its loads appended.  RosettaMath's `loader_eq.py`
proves `admit_accepts`, `admit_disjoint` and `admit_ordered`, Lean-accepted.  The legs of
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
import elfcheck                                          # noqa: E402
import loader                                            # noqa: E402
# `loader.py` reaches `accept_image` and `regions_disjoint` by textual
# inclusion after `elfcheck.py` and `memmap.py`; run as Python it needs the
# same names in scope.
loader.accept_image = elfcheck.accept_image
loader.regions_disjoint = memmap.regions_disjoint
from tests.test_crustos_model import (                   # noqa: E402
    _rosettamath, _in_big_stack, _nat, _skeleton, _body_of)
from tests.test_elfcheck_model import LEAN                # noqa: E402
from tests.test_memmap_model import LAYOUT                # noqa: E402

ROSETTAMATH = _rosettamath()
B, S, O = LAYOUT       # kernel [0,16), t0 stack [16,32), t0 slots [32,36),
                       # t1 stack [36,52), t1 slots [52,56)

# (tag, vaddrs, memszs, entry, cls, guest).  The layout ends at 56.
CORPUS = [
    ("above",     [64, 80], [16, 8], 70, 1, 3),
    ("touch",     [56, 64], [8, 8],  60, 1, 3),    # first load starts at 56: fine
    ("overlap",   [50, 64], [8, 8],  52, 1, 3),    # runs into thread 1's stack
    ("inside",    [20, 64], [4, 8],  22, 1, 3),    # entirely inside thread 0's stack
    ("bad_cls",   [64, 80], [16, 8], 70, 4, 3),
    ("entry_out", [64, 80], [16, 8], 100, 1, 3),
    ("descend",   [80, 64], [8, 8],  82, 1, 3),
    ("empty",     [], [], 0, 0, 3),               # no loads: entry in none
    ("one",       [64], [16], 79, 0, 3),
    ("one_end",   [64], [16], 80, 0, 3),
]


def _python(row):
    _, v, m, e, cls, guest = row
    nb = loader.extended(B, v)
    own = loader.claimed(O, guest, len(v))
    return (loader.admit(B, S, v, m, e, cls), len(nb), nb[-1] if nb else 0,
            len(own), own[-1])


_MODEL = {}


def _load_model():
    if _MODEL:
        return _MODEL
    sys.path.insert(0, ROSETTAMATH)

    def work():
        import loader_eq
        import hoare
        import lean4
        env, facts, model = loader_eq.build()
        return env, facts, model, hoare, lean4

    env, facts, model, hoare, lean4 = _in_big_stack(work)
    _MODEL.update(env=env, facts=facts, model=model, hoare=hoare, lean4=lean4)
    return _MODEL


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestLoaderAgrees(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loaded = _load_model()
        cls.env, cls.H, cls.L = loaded["env"], loaded["hoare"], loaded["lean4"]
        cls.procs = {k: v.lean_procedure for k, v in loaded["model"].items()}

    def model(self, row):
        H, L, env = self.H, self.L, self.env
        _, v, m, e, cls, guest = row
        ab, asz, ao, av, am = H.array(B), H.array(S), H.array(O), H.array(v), H.array(m)
        num = L.numeral
        grown = H.app(self.procs["extended"].fn_term, ab, av)
        own = H.app(self.procs["claimed"].fn_term, ao, num(guest), num(len(v)))
        length = lambda xs: H.app('len', H.NAT, xs)
        last = lambda xs: H.app('nth', H.NAT, num(0), xs,
                                H.app('sub', length(xs), num(1)))
        terms = [
            H.app(self.procs["admit"].fn_term, ab, asz, av, am, num(e), num(cls)),
            length(grown), last(grown), length(own), last(own),
        ]
        return _in_big_stack(lambda: tuple(_nat(t, env, L) for t in terms))

    def test_agrees_with_the_model(self):
        for row in CORPUS:
            with self.subTest(case=row[0]):
                self.assertEqual(_python(row), self.model(row),
                                 "loader and its model disagree on %r" % (row,))

    def test_admitted_loads_keep_the_founding_rule(self):
        # admit_ordered, by hand: an admitted image leaves the grown list
        # disjoint, and no address is reachable by two owners
        for row in CORPUS:
            _, v, m, e, cls, guest = row
            if loader.admit(B, S, v, m, e, cls) != 1:
                continue
            nb, ns = loader.extended(B, v), loader.extended(S, m)
            own = loader.claimed(O, guest, len(v))
            self.assertEqual(memmap.regions_disjoint(nb, ns), 1)
            for addr in range(0, 100):
                who = [w for w in range(0, 5) if memmap.region_of(nb, ns, own, w, addr)]
                self.assertLessEqual(len(who), 1, (row[0], addr, who))
            self.assertEqual(memmap.region_of(nb, ns, own, guest, e), 1,
                             "the guest cannot reach its own entry")


class TestCompiledAgrees(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp()
        for name in ("memmap.py", "elfcheck.py", "loader.py"):
            shutil.copy(os.path.join(ROOT, "leanos", name), d)
        width = max(max(len(r[1]), 1) for r in CORPUS)
        c = ["#include <stdio.h>", '#include "memmap.py"', '#include "elfcheck.py"',
             '#include "loader.py"',
             "static long ba[5] = {%s}, sz[5] = {%s}; static int ow[5] = {%s};"
             % (",".join(map(str, B)), ",".join(map(str, S)), ",".join(map(str, O))),
             "static long va[%d], ms[%d];" % (width, width),
             "static _tlist_long LB = { ba, 5, 5 }, LS = { sz, 5, 5 }, LV = { va, 0, %d }, LM = { ms, 0, %d };" % (width, width),
             "static _tlist_int LO = { ow, 5, 5 };",
             "int main() {"]
        for tag, v, m, e, rc, guest in CORPUS:
            for k, x in enumerate(v): c.append("  va[%d] = %dL;" % (k, x))
            for k, x in enumerate(m): c.append("  ms[%d] = %dL;" % (k, x))
            c.append("  LV.len = %d; LM.len = %d;" % (len(v), len(m)))
            c.append("  { _tlist_long *nb = extended(&LB, &LV); _tlist_int *own = claimed(&LO, %d, %d);" % (guest, len(v)))
            c.append('    printf("%s %%d %%ld %%ld %%ld %%d\\n", admit(&LB,&LS,&LV,&LM,%dL,%d),'
                     ' nb->len, nb->len ? nb->data[nb->len-1] : 0, own->len, own->data[own->len-1]); }'
                     % (tag, e, rc))
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


MODELLED = {"extended", "claimed", "admit"}


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestModelShape(unittest.TestCase):
    def test_modelled_functions_are_the_same_function_twice(self):
        _load_model()
        import loader_eq
        for name in sorted(MODELLED):
            with self.subTest(name=name):
                real = _skeleton(_body_of(inspect.getsource(getattr(loader, name))))
                self.assertEqual(real, _skeleton(_body_of(loader_eq.SOURCES[name])))


class TestModelCoverage(unittest.TestCase):
    def test_every_exported_function_is_accounted_for(self):
        exported = {n for n, v in vars(loader).items() if callable(v)
                    and not n.startswith("_")
                    and getattr(v, "__module__", None) == loader.__name__}
        self.assertEqual(exported, MODELLED)


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
@unittest.skipUnless(LEAN, "no lean on PATH; run 'make install_lean'")
class TestLeanAccepts(unittest.TestCase):
    def test_all_three_with_no_axioms(self):
        loaded = _load_model()
        import loader_eq
        src = _in_big_stack(lambda: loader_eq.lean_source(loaded["env"]))
        path = os.path.join(tempfile.mkdtemp(), "Loader.lean")
        with open(path, "w") as fh:
            fh.write(src)
        run = subprocess.run([LEAN, path], capture_output=True, text=True, timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for name in loader_eq.THEOREMS:
            self.assertIn("'RM.%s' does not depend on any axioms" % name, run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
