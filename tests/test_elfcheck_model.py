"""The ELF validator and its model, checked against each other.

`crustos/elfcheck.py` decides whether the loader may map an ELF.  RosettaMath's
`elfcheck_eq.py` is the same four functions written for the proof kernel, with
two theorems -- `reg_class_sized`, `accept_sized` -- that Lean 4 also checks.
Same discipline as `test_crustos_model.py`, with one more leg:

  Behaviour.   The rpython, run as Python, and the *compiled model terms*,
               evaluated through the kernel, agree over a corpus.
  Compiled.    The rpython, compiled by ShivyCX through `#include`, agrees
               with itself run as Python.  The scheme layer has no such test;
               this file is included by `kernel.c` the same way and the
               loader will call it, so what the compiler makes of it is part
               of the claim.
  Shape.       The two sources have the same control-flow skeleton.
  Coverage.    Every function the validator exports is modelled.
  Loader.      `elf.c` with the validator wired in, on a real ELF and on
               copies with corrupted headers, each refused before mapping.
  Lean.        The exported theorems are accepted with no axioms.  Skipped
               without `lean`, as in `test_ilproof.py`.

The corpus uses small addresses.  The model is over unary Nat, and a
page-sized address is a term thousands deep; the compiled leg runs real
page-scale cases as well, since C does not care.
"""
import ast
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
sys.path.insert(0, os.path.join(ROOT, "crustos"))

import elfcheck                                          # noqa: E402
from tests.test_crustos_model import (                   # noqa: E402
    _rosettamath, _in_big_stack, _nat, _skeleton, _body_of)

ROSETTAMATH = _rosettamath()
LEAN = shutil.which("lean") or next(
    (p for p in (os.path.expanduser("~/.local/lean/bin/lean"),
                 os.path.expanduser("~/.elan/bin/lean"))
     if os.path.exists(p)), None)

# (tag, vaddrs, memszs, entry, cls).  Every branch of every function is
# taken by at least one row; the shape test is what says no branch is
# missing, this is what says each one was exercised.
CORPUS = [
    ("ok",         [16, 32], [16, 21], 20, 1),
    ("entry_lo",   [16, 32], [16, 21], 15, 1),      # below the first load
    ("entry_hi",   [16, 32], [16, 21], 53, 1),      # == end: outside
    ("entry_edge", [16, 32], [16, 21], 52, 3),      # end - 1: inside
    ("cls_max",    [16, 32], [16, 21], 20, 3),
    ("cls_bad",    [16, 32], [16, 21], 20, 4),
    ("short_ms",   [16, 32], [16],     20, 1),      # memszs too short
    ("long_ms",    [16, 32], [16, 21, 9], 20, 1),   # extra memsz is ignored
    ("empty",      [],       [],       0,  0),
    ("overlap",    [16, 24], [16, 21], 20, 1),      # second starts inside first
    ("touch",      [16, 32], [16, 21], 32, 2),      # end == next start: fine
    ("descend",    [80, 16], [1, 1],   17, 0),
    ("one",        [5],      [3],      7,  0),
    ("one_edge",   [5],      [3],      8,  0),
    ("zero_size",  [16, 16], [0, 4],   16, 1),      # empty load, then a real one
]
PAGE_CORPUS = [
    ("page_ok",      [0x1000, 0x2000], [0x1000, 0x1500], 0x1234, 1),
    ("high_va",      [0x7f0000001000, 0x7f0000002000], [0x1000, 0x1500],
                     0x7f0000002010, 1),
    ("page_overlap", [0x1000, 0x1800], [0x1000, 0x1500], 0x1234, 1),
    ("page_edge",    [0x1000, 0x2000], [0x1000, 0x1500], 0x34ff, 3),
    ("page_out",     [0x1000, 0x2000], [0x1000, 0x1500], 0x3500, 3),
]


def _python(row):
    _, v, m, e, c = row
    return (elfcheck.loads_ordered(v, m), elfcheck.entry_in_load(v, m, e),
            elfcheck.reg_class_ok(c), elfcheck.accept_image(v, m, e, c))


_MODEL = {}


def _load_model():
    if _MODEL:
        return _MODEL
    sys.path.insert(0, ROSETTAMATH)

    def work():
        import elfcheck_eq
        import hoare
        import lean4
        env, facts, model = elfcheck_eq.build()
        return env, facts, model, hoare, lean4

    env, facts, model, hoare, lean4 = _in_big_stack(work)
    _MODEL.update(env=env, facts=facts, model=model, hoare=hoare, lean4=lean4)
    return _MODEL


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestValidatorAgrees(unittest.TestCase):
    """Behaviour: the rpython and the compiled model terms, one corpus."""

    @classmethod
    def setUpClass(cls):
        loaded = _load_model()
        cls.env, cls.hoare, cls.lean4 = (loaded["env"], loaded["hoare"],
                                         loaded["lean4"])
        cls.procs = {name: fn.lean_procedure
                     for name, fn in loaded["model"].items()}

    def model(self, row):
        H, L, env = self.hoare, self.lean4, self.env
        _, v, m, e, c = row
        av, am = H.array(v), H.array(m)
        terms = [
            H.app(self.procs["loads_ordered"].fn_term, av, am),
            H.app(self.procs["entry_in_load"].fn_term, av, am, L.numeral(e)),
            H.app(self.procs["reg_class_ok"].fn_term, L.numeral(c)),
            H.app(self.procs["accept_image"].fn_term, av, am, L.numeral(e),
                  L.numeral(c)),
        ]
        return _in_big_stack(lambda: tuple(_nat(t, env, L) for t in terms))

    def test_model_evaluates_to_numbers(self):
        for row in CORPUS:
            with self.subTest(case=row[0]):
                self.assertNotIn(None, self.model(row))

    def test_agrees_with_the_model(self):
        for row in CORPUS:
            with self.subTest(case=row[0]):
                self.assertEqual(
                    _python(row), self.model(row),
                    "the validator and its model disagree on %r "
                    "(loads_ordered, entry_in_load, reg_class_ok, accept_image). "
                    "One of them is wrong and it is not safe to guess which."
                    % (row,))

    def test_the_theorems_are_about_these_terms(self):
        # `accept_sized` quantifies over the same `accept` the corpus ran.
        facts = _load_model()["facts"]
        self.assertIn("accept_sized", facts)
        self.assertIn("accept_image", self.lean4.readable(facts["accept_sized"]))


class TestCompiledAgrees(unittest.TestCase):
    """Compiled: the same file through ShivyCX, as `kernel.c` will take it."""

    @classmethod
    def setUpClass(cls):
        rows = CORPUS + PAGE_CORPUS
        d = tempfile.mkdtemp()
        shutil.copy(os.path.join(ROOT, "crustos", "elfcheck.py"), d)
        width = max(max(len(r[1]), len(r[2])) for r in rows) or 1
        c = ["#include <stdio.h>", '#include "elfcheck.py"',
             "static long va[%d], ms[%d];" % (width, width),
             "static _tlist_long LV = { va, 0, %d }, LM = { ms, 0, %d };"
             % (width, width),
             "int main() {"]
        for tag, v, m, e, k in rows:
            for i, x in enumerate(v):
                c.append("  va[%d] = %dL;" % (i, x))
            for i, x in enumerate(m):
                c.append("  ms[%d] = %dL;" % (i, x))
            c.append("  LV.len = %d; LM.len = %d;" % (len(v), len(m)))
            c.append('  printf("%s %%d %%d %%d %%d\\n", loads_ordered(&LV, &LM),'
                     ' entry_in_load(&LV, &LM, %dL), reg_class_ok(%d),'
                     ' accept_image(&LV, &LM, %dL, %d));' % (tag, e, k, e, k))
        c.append("  return 0; }")
        with open(os.path.join(d, "t.c"), "w") as fh:
            fh.write("\n".join(c))
        proc = subprocess.run(
            [sys.executable, "-m", "shivyc.main", "--no-cache",
             os.path.join(d, "t.c"), "-o", os.path.join(d, "t")],
            capture_output=True, text=True, cwd=ROOT)
        assert proc.returncode == 0, proc.stderr[-1200:]
        run = subprocess.run([os.path.join(d, "t")], capture_output=True,
                             text=True)
        cls.compiled = {}
        for line in run.stdout.splitlines():
            parts = line.split()
            cls.compiled[parts[0]] = tuple(int(x) for x in parts[1:])
        cls.rows = rows

    def test_every_case_ran(self):
        self.assertEqual(set(self.compiled), {r[0] for r in self.rows})

    def test_compiled_agrees_with_python(self):
        for row in self.rows:
            with self.subTest(case=row[0]):
                self.assertEqual(
                    _python(row), self.compiled[row[0]],
                    "ShivyCX and CPython disagree about elfcheck on %r"
                    % (row,))


class TestLoaderRefuses(unittest.TestCase):
    """End to end: `elf.c` with the validator wired in, on real ELF files.

    A ShivyCX-built program is loaded as-is, then with its headers patched
    four ways.  Before the validator, `elf_load_path` returned 0 for every
    one of these and `elf_run_guest_fn` would have jumped to the entry it
    was given -- for `entry_out`, a megabyte past a 16 KB image.
    """

    @classmethod
    def setUpClass(cls):
        import struct
        d = tempfile.mkdtemp()
        for name in ("elfcheck.py", "elf.c"):
            shutil.copy(os.path.join(ROOT, "crustos", name), d)
        with open(os.path.join(d, "h.c"), "w") as fh:
            fh.write('#include <stdio.h>\n#include <stdlib.h>\n'
                     '#include <string.h>\n#include "elfcheck.py"\n'
                     '#include "elf.c"\n'
                     'int main(int c, char **v) { struct ElfImage img;\n'
                     '  int rc = elf_load_path(v[1], &img);\n'
                     '  printf("%d\\n", rc); if (rc == 0) elf_free(&img);\n'
                     '  return 0; }\n')
        with open(os.path.join(d, "guest.c"), "w") as fh:
            fh.write("int main() { return 0; }\n")
        for src, out in (("h.c", "h"), ("guest.c", "guest")):
            proc = subprocess.run(
                [sys.executable, "-m", "shivyc.main", "--no-cache",
                 os.path.join(d, src), "-o", os.path.join(d, out)],
                capture_output=True, text=True, cwd=ROOT)
            assert proc.returncode == 0, proc.stderr[-1200:]
        cls.loader = os.path.join(d, "h")
        with open(os.path.join(d, "guest"), "rb") as fh:
            cls.guest = fh.read()
        cls.dir = d
        b = cls.guest
        phoff = struct.unpack_from("<Q", b, 0x20)[0]
        phentsize, phnum = struct.unpack_from("<HH", b, 0x36)
        cls.loads = [phoff + i * phentsize for i in range(phnum)
                     if struct.unpack_from("<I", b, phoff + i * phentsize)[0] == 1]
        assert len(cls.loads) >= 2

    def load(self, name, edits=()):
        import struct
        b = bytearray(self.guest)
        for off, fmt, val in edits:
            struct.pack_into(fmt, b, off, val)
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(b)
        run = subprocess.run([self.loader, path], capture_output=True,
                             text=True)
        return int(run.stdout.strip())

    def vaddr(self, k):
        import struct
        return struct.unpack_from("<Q", self.guest, self.loads[k] + 16)[0]

    def memsz(self, k):
        import struct
        return struct.unpack_from("<Q", self.guest, self.loads[k] + 40)[0]

    def test_a_well_formed_image_is_accepted(self):
        self.assertEqual(self.load("ok"), 0)

    def test_overlapping_loads_are_refused(self):
        self.assertEqual(self.load("overlap", [
            (self.loads[1] + 16, "<Q", self.vaddr(0) + 0x100)]), -6)

    def test_descending_loads_are_refused(self):
        self.assertEqual(self.load("descend", [
            (self.loads[0] + 16, "<Q", self.vaddr(1)),
            (self.loads[1] + 16, "<Q", self.vaddr(0))]), -6)

    def test_entry_outside_every_load_is_refused(self):
        self.assertEqual(self.load("entry_out", [
            (0x18, "<Q", self.vaddr(-1) + self.memsz(-1) + 0x100000)]), -6)

    def test_entry_at_the_end_of_a_load_is_refused(self):
        end = self.vaddr(-1) + self.memsz(-1)
        self.assertEqual(self.load("entry_end", [(0x18, "<Q", end)]), -6)
        self.assertEqual(self.load("entry_last", [(0x18, "<Q", end - 1)]), 0)


MODELLED = {"reg_class_ok", "loads_ordered", "entry_in_load", "accept_image"}
UNMODELLED = {}
EXPECTED_SHAPE_GAPS = {}


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestModelShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _load_model()
        import elfcheck_eq
        cls.sources = elfcheck_eq.SOURCES

    def test_modelled_functions_are_the_same_function_twice(self):
        for name in sorted(MODELLED - set(EXPECTED_SHAPE_GAPS)):
            with self.subTest(name=name):
                real = _skeleton(_body_of(inspect.getsource(
                    getattr(elfcheck, name))))
                model = _skeleton(_body_of(self.sources[name]))
                self.assertEqual(real, model,
                                 "%s: the model no longer has the shape of "
                                 "the shipped function" % name)


class TestModelCoverage(unittest.TestCase):
    def test_every_exported_function_is_accounted_for(self):
        exported = {name for name, value in vars(elfcheck).items()
                    if callable(value) and not name.startswith("_")
                    and getattr(value, "__module__", None)
                    == elfcheck.__name__}
        self.assertEqual(exported - MODELLED - set(UNMODELLED), set())
        self.assertEqual(MODELLED - exported, set())


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
@unittest.skipUnless(LEAN, "no lean on PATH; run 'make install_lean'")
class TestLeanAccepts(unittest.TestCase):
    def test_both_theorems_with_no_axioms(self):
        loaded = _load_model()
        import elfcheck_eq
        src = _in_big_stack(lambda: elfcheck_eq.lean_source(loaded["env"]))
        path = os.path.join(tempfile.mkdtemp(), "ElfCheck.lean")
        with open(path, "w") as fh:
            fh.write(src)
        run = subprocess.run([LEAN, path], capture_output=True, text=True,
                             timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for name in ("loads_ordered_bounded", "entry_in_load_bounded",
                     "reg_class_sized", "accept_sized"):
            self.assertIn("'RM.%s' does not depend on any axioms" % name,
                          run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
