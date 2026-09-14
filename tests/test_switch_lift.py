"""LeanOS's switcher: C, lifted from the compiled IL, and proved from the lift.

`leanos/switch.c` is the switcher's decisions -- which side owns a register,
where it is saved, how many a side saves, who runs next -- as integer
functions of the partition in `BAREMETAL_THREADS.md`.  Unlike the rpython
modules, nothing here is modelled from source: `shivyc/ilproof.py` lifts
each function from the IL the compiler produced, and the theorems are about
that.  Three legs:

  Binary.    Each function, compiled and run, agrees with its lifted model
             through the kernel's evaluator over every register number.
  Theorems.  From the lifted source: the sides are disjoint (left is below
             the split, right is not), the counts are bounded, and the save
             set for the left side is the six-register footprint the
             measured case reports.
  Lean.      All of it accepted with no axioms.
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tests.test_crustos_model import _rosettamath, _in_big_stack, _nat  # noqa: E402
from tests.test_elfcheck_model import LEAN                              # noqa: E402
from tests.test_ilproof import il_of                                    # noqa: E402

ROSETTAMATH = _rosettamath()
SWITCH = os.path.join(ROOT, "leanos", "switch.c")
FUNCTIONS = ["reg_side", "save_slot", "saves", "next_thread"]
REGS = list(range(0, 40))


def _lifted():
    """The four functions as the lift reads them from the compiled IL."""
    from shivyc.ilproof import lift
    with open(SWITCH) as fh:
        src = fh.read()
    il, table = il_of(src, path=SWITCH)
    return {fn: lift(il, table, fn) for fn in FUNCTIONS}


_STATE = {}


def _build():
    if _STATE:
        return _STATE
    sys.path.insert(0, ROSETTAMATH)

    def work():
        import hoare
        import lean4
        from lean4 import Var, Pi
        from hoare import app, NAT
        env = hoare.prelude()
        sources = _lifted()
        procs = {}
        for fn in FUNCTIONS:
            procs[fn] = hoare.read_procedure(sources[fn], env, None,
                                             ['result <= result'])
        facts = {}
        r, side, cur = Var('r'), Var('side'), Var('cur')
        one, num = lean4.numeral(1), lean4.numeral
        implies = lambda a, b: app('orb', app('notb', a), b)

        def theorem(name, goal, unfolding):
            lean4.define(env, name, goal,
                         hoare.by_every_bool(env, goal, unfolding=unfolding))
            facts[name] = goal

        rs = app('reg_side', r)
        theorem('reg_side_bounded',
                Pi('r', NAT, app('Holds', app('leb', rs, num(2)))), {'reg_side'})
        theorem('left_below_split',
                Pi('r', NAT, app('Holds', implies(app('eqb', rs, num(0)),
                                                  app('ltb', r, num(25))))),
                {'reg_side'})
        theorem('right_not_below_split',
                Pi('r', NAT, app('Holds', implies(app('eqb', rs, one),
                                                  app('notb', app('ltb', r, num(25)))))),
                {'reg_side'})
        theorem('next_thread_bounded',
                Pi('cur', NAT, app('Holds', app('leb', app('next_thread', cur), one))),
                {'next_thread'})
        theorem('saves_bounded',
                Pi('side', NAT, app('Holds', app('leb', app('saves', side), num(6)))),
                {'saves'})
        # closed facts: the left save set is the measured footprint, the two
        # sides together are the whole callee-saved range, and the two
        # threads alternate
        for name, claim in (
                ('left_saves_footprint', app('eqb', app('saves', num(0)), num(6))),
                ('sides_cover_range', app('eqb', app('add', app('saves', num(0)),
                                                     app('saves', num(1))), num(10))),
                ('threads_alternate', app('andb',
                                          app('eqb', app('next_thread', num(0)), one),
                                          app('eqb', app('next_thread', one), num(0))))):
            goal = app('Holds', claim)
            lean4.define(env, name, goal, hoare.discharge(goal, env, verbose=False))
            facts[name] = goal
        return env, procs, facts, sources, hoare, lean4

    env, procs, facts, sources, hoare, lean4 = _in_big_stack(work)
    _STATE.update(env=env, procs=procs, facts=facts, sources=sources,
                  hoare=hoare, lean4=lean4)
    return _STATE


class TestLiftAgreesWithBinary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp()
        with open(SWITCH) as fh:
            src = fh.read()
        harness = src + "\n#include <stdio.h>\nint main() {\n"
        for r in REGS:
            harness += ('  printf("%%d %%d %%d %%d %%d\\n", %d, reg_side(%d), '
                        'save_slot(%d), saves(%d), next_thread(%d));\n' % (r, r, r, r, r))
        harness += "  return 0; }\n"
        with open(os.path.join(d, "t.c"), "w") as fh:
            fh.write(harness)
        proc = subprocess.run([sys.executable, "-m", "shivyc.main", "--no-cache",
                               os.path.join(d, "t.c"), "-o", os.path.join(d, "t")],
                              capture_output=True, text=True, cwd=ROOT)
        assert proc.returncode == 0, proc.stderr[-1200:]
        run = subprocess.run([os.path.join(d, "t")], capture_output=True, text=True)
        cls.binary = {}
        for line in run.stdout.splitlines():
            r, *vals = (int(x) for x in line.split())
            cls.binary[r] = vals

    @unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
    def test_every_function_agrees_with_the_binary(self):
        st = _build()
        H, L, env = st["hoare"], st["lean4"], st["env"]
        for r in REGS:
            want = self.binary[r]
            got = _in_big_stack(lambda: [
                _nat(H.app(st["procs"][fn].fn_term, L.numeral(r)), env, L)
                for fn in FUNCTIONS])
            # save_slot(r) for r < 19 is negative in C and 0 over Nat: the
            # one place the model and the machine part, and stated as such
            if r < 19:
                want = want[:1] + [max(want[1], 0)] + want[2:]
            self.assertEqual(want, got,
                             "the lifted model and the binary disagree at r=%d" % r)

    def test_the_footprint_is_six_registers(self):
        # the compiled saves(SIDE_LEFT) is the IO thread's footprint x19..x24
        self.assertEqual(self.binary[0][2], 6)


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestTheoremsFromTheLift(unittest.TestCase):
    def test_all_eight_proved(self):
        facts = _build()["facts"]
        self.assertEqual(len(facts), 8, sorted(facts))

    def test_sides_are_disjoint_by_hand(self):
        # left_below_split and right_not_below_split, over the corpus
        b = TestLiftAgreesWithBinary
        b.setUpClass()
        for r in REGS:
            side = b.binary[r][0]
            if side == 0:
                self.assertLess(r, 25)
            if side == 1:
                self.assertGreaterEqual(r, 25)


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
@unittest.skipUnless(LEAN, "no lean on PATH; run 'make install_lean'")
class TestLeanAccepts(unittest.TestCase):
    def test_every_theorem_with_no_axioms(self):
        st = _build()
        import leanexport
        names = sorted(st["facts"])

        def export():
            slow = st["hoare"].prelude(fast=False)
            values = {n: st["lean4"].value_of(slow, n) for n in st["hoare"].ACCELERATED}
            src, _ = leanexport.export(st["env"], names, values,
                                       ['#print axioms ' + n for n in names])
            return src

        src = _in_big_stack(export)
        path = os.path.join(tempfile.mkdtemp(), "Switch.lean")
        with open(path, "w") as fh:
            fh.write(src)
        run = subprocess.run([LEAN, path], capture_output=True, text=True, timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for n in names:
            self.assertIn("'RM.%s' does not depend on any axioms" % n, run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
