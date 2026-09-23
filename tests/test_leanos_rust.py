"""LeanOS's region list, loader checks, threads and loader, ported to Rust.

`leanos/memmap.rs`, `elfcheck.rs`, `threads.rs` and `loader.rs` are ports of
the rpython files of the same names.  Each is held to the original the way
`alloc.rs` is (`test_rustproof.py`, section 6):

  * it lifts, every function, through `load_unit` -- a file and what its
    `// uses:` line names, as one unit;
  * compiled by Crust and run, it answers as the Python does over the
    Python model test's own corpus, row for row;
  * every obligation `tools/rustprove.py` states is proved -- the contracts,
    and every place the Rust could panic -- and the open set is pinned;
  * with Lean 4 on PATH, every proved theorem is accepted by Lean too.

The ports differ from the Python only where the Python's arithmetic passes
2^64 (see each file's header); the corpora stay below that, so the answers
must agree exactly.  Where the difference is the point -- an address sum
that would wrap -- a row of its own says what the Rust answers.
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "leanos"))

from shivyc.rustproof import lift_all                    # noqa: E402
from tests.test_rustproof import (                       # noqa: E402
    ROSETTAMATH, LEAN, _compile_and_print, _in_big_stack)
import rustprove                                          # noqa: E402

import memmap                                             # noqa: E402
import elfcheck                                           # noqa: E402
from tests import test_memmap_model as MM                 # noqa: E402
from tests import test_elfcheck_model as EM               # noqa: E402
from tests import test_threads_model as TM                # noqa: E402
from tests import test_loader_model as LM                 # noqa: E402

LEANOS = os.path.join(ROOT, "leanos")
MODULES = {
    "memmap": ["regions_disjoint", "contains", "owned_by", "region_index",
               "region_of"],
    "elfcheck": ["reg_class_ok", "loads_ordered", "entry_in_load",
                 "accept_image"],
    "threads": ["thread_owner", "sp_ok", "all_sps_ok", "sp_after_push"],
    "loader": ["joined", "regions_disjoint_joined", "admit"],
}

# Every obligation the kernel does not settle, per module: none.  A new
# open obligation fails this, so one cannot appear quietly.
OPEN = {name: set() for name in MODULES}


def _unit(name):
    return rustprove.load_unit(os.path.join(LEANOS, name + ".rs"))


def _u64s(xs):
    # an empty literal has no element type for Crust; an empty range of a
    # one-element array is the same empty slice, typed
    if not xs:
        return "&[0u64][..0]"
    return "&[%s][..]" % ", ".join("%du64" % x for x in xs)


def _usizes(xs):
    if not xs:
        return "&[0usize][..0]"
    return "&[%s][..]" % ", ".join("%dusize" % x for x in xs)


class TestLift(unittest.TestCase):

    def test_every_function_lifts(self):
        for name, fns in MODULES.items():
            with self.subTest(module=name):
                source, own = _unit(name)
                ok, refused = lift_all(source)
                self.assertEqual({k: v for k, v in refused.items()
                                  if k in own}, {})
                self.assertEqual(sorted(n for n in ok if n in own),
                                 sorted(fns))

    def test_a_unit_keeps_its_own_line_numbers(self):
        # the used files come after, so line N of threads.rs is line N
        source, _ = _unit("threads")
        with open(os.path.join(LEANOS, "threads.rs")) as fh:
            mine = fh.read()
        self.assertTrue(source.startswith(mine))


class TestAgreesWithPython(unittest.TestCase):
    """Compiled by Crust and run, over each Python model test's corpus."""

    def test_memmap(self):
        calls = []
        for _, b, s, o, who, addr, i in MM.CORPUS:
            calls += [
                "regions_disjoint(%s, %s)" % (_u64s(b), _u64s(s)),
                "contains(%s, %s, %d, %d)" % (_u64s(b), _u64s(s), i, addr),
                "owned_by(%s, %d)" % (_usizes(o), who),
                "region_of(%s, %s, %s, %d, %d)" % (_u64s(b), _u64s(s),
                                                  _usizes(o), who, addr),
            ]
        got = _compile_and_print(_unit("memmap")[0], calls)
        for k, row in enumerate(MM.CORPUS):
            with self.subTest(case=row[0]):
                self.assertEqual(tuple(got[4 * k:4 * k + 4]), MM._python(row))

    def test_elfcheck(self):
        calls = []
        for _, v, m, e, c in EM.CORPUS:
            calls += [
                "loads_ordered(%s, %s)" % (_u64s(v), _u64s(m)),
                "entry_in_load(%s, %s, %d)" % (_u64s(v), _u64s(m), e),
                "reg_class_ok(%d)" % c,
                "accept_image(%s, %s, %d, %d)" % (_u64s(v), _u64s(m), e, c),
            ]
        got = _compile_and_print(_unit("elfcheck")[0], calls)
        for k, row in enumerate(EM.CORPUS):
            with self.subTest(case=row[0]):
                self.assertEqual(tuple(got[4 * k:4 * k + 4]), EM._python(row))

    def test_threads(self):
        B, S, O = TM.B, TM.S, TM.O
        regions = "%s, %s, %s" % (_u64s(B), _u64s(S), _usizes(O))
        calls = []
        for _, sps, tid, sp, n in TM.CORPUS:
            calls += [
                "sp_ok(%s, %d, %d)" % (regions, tid, sp),
                "all_sps_ok(%s, %s)" % (regions, _u64s(sps)),
                "sp_after_push(%s, %d, %d, %d)" % (regions, tid, sp, n),
                "thread_owner(%d)" % tid,
            ]
        got = _compile_and_print(_unit("threads")[0], calls)
        for k, row in enumerate(TM.CORPUS):
            with self.subTest(case=row[0]):
                self.assertEqual(tuple(got[4 * k:4 * k + 4]), TM._python(row))

    def test_loader(self):
        B, S = LM.B, LM.S
        calls = []
        for _, v, m, e, cls, _guest in LM.CORPUS:
            calls += [
                "admit(%s, %s, %s, %s, %d, %d)" % (_u64s(B), _u64s(S),
                                                  _u64s(v), _u64s(m), e, cls),
                "regions_disjoint_joined(%s, %s, %s, %s)" % (
                    _u64s(B), _u64s(S), _u64s(v), _u64s(m)),
            ]
        got = _compile_and_print(_unit("loader")[0], calls)
        for k, row in enumerate(LM.CORPUS):
            _, v, m, e, cls, _guest = row
            with self.subTest(case=row[0]):
                self.assertEqual(got[2 * k], LM._python(row)[0])
                # reading the concatenation in place is building it
                self.assertEqual(got[2 * k + 1], memmap.regions_disjoint(
                    B + v, S + m))

    def test_what_would_have_wrapped(self):
        """The rows where the Python's integers pass 2^64: the port refuses
        rather than wraps."""
        top = (1 << 64) - 1
        source, _ = _unit("loader")
        got = _compile_and_print(source, [
            # a region ending at the top, then one after it: not disjoint,
            # though `base + size` wraps to 15 and would say it was
            "regions_disjoint(%s, %s)" % (_u64s([top - 15, 1]),
                                          _u64s([16, 4])),
            # a load running past the top of the address space
            "loads_ordered(%s, %s)" % (_u64s([top - 3]), _u64s([8])),
            "entry_in_load(%s, %s, %d)" % (_u64s([top - 3]), _u64s([8]), 2),
        ])
        self.assertEqual(got, [0, 0, 0])
        # the Python agrees on the first and third, having no wrap to fall
        # into; on the second it accepts a load no loader could map
        self.assertEqual(memmap.regions_disjoint([top - 15, 1], [16, 4]), 0)
        self.assertEqual(elfcheck.loads_ordered([top - 3], [8]), 1)


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestProved(unittest.TestCase):
    """Every obligation, in every module, settled by the kernel."""

    @classmethod
    def setUpClass(cls):
        cls.provers, cls.open = {}, {}
        for name in MODULES:
            prover = rustprove.Prover(*_unit(name))
            open_ = set()

            def work(prover=prover, open_=open_):
                for fn in sorted(n for n in prover.lifted if n in prover.own):
                    f = prover.lifted[fn]
                    if f.ensures and not prover.contract(fn):
                        open_.add((fn, "ensures"))
                    for label, ok in prover.safety(fn):
                        if not ok:
                            open_.add((fn, label))
            _in_big_stack(work)
            cls.provers[name], cls.open[name] = prover, open_

    def test_nothing_is_open(self):
        for name in MODULES:
            with self.subTest(module=name):
                self.assertEqual(self.open[name], OPEN[name])

    def test_the_theorems_the_hand_models_had(self):
        """RosettaMath's `*_eq.py` proved these about hand-typed models;
        here each is a contract, and settled, about the lifted source."""
        wanted = {
            "elfcheck": {("reg_class_ok", "reg_class_sized"),
                         ("accept_image", "accept_sized")},
            "threads": {("sp_after_push", "push_keeps_sp_ok")},
            "loader": {("admit", "admit_accepts and admit_disjoint")},
        }
        for name, pairs in wanted.items():
            certified = {c.function for c in self.provers[name].certificates
                         if c.label.startswith("ensures")}
            for fn, _theorem in pairs:
                with self.subTest(module=name, theorem=_theorem):
                    self.assertIn(fn, certified)

    @unittest.skipUnless(LEAN, "lean not on PATH")
    def test_lean_agrees_with_every_certificate(self):
        import rustlean
        for name in MODULES:
            certs = self.provers[name].certificates
            out = tempfile.mkdtemp()
            verdicts = _in_big_stack(
                lambda: rustlean.check(certs, out, lean=LEAN))
            with self.subTest(module=name):
                self.assertEqual([v.name for v in verdicts if not v.agreed],
                                 [])
                self.assertEqual(len(verdicts), len(certs))


if __name__ == "__main__":
    unittest.main()
