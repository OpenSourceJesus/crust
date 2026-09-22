"""The IL lift, checked two ways: against the binary, and by the kernel.

`shivyc/ilproof.py` turns a compiled function's IL back into the Python
fragment `hoare.py` proves things about.  Two questions follow, and they are
tested separately because they fail separately:

  1. Is the lifted model the function?  Each example is compiled with the
     real compiler and run, and the lifted term is evaluated through the
     proof kernel on the same inputs.  A disagreement here is a bug in the
     lift, the same way a disagreement in `test_crustos_model.py` is a bug
     in the model.

  2. Can the kernel prove something about it?  Termination of the counted
     loop, for every bound; a result bound on the guard chain, for every
     input; and, for the one function that is real kernel code, the same
     bound accepted by Lean 4.

Skipped without RosettaMath, as the model tests are.  The Lean check is
skipped separately when `lean` is not on PATH, since it is a second kernel
and not the first: what it adds is independence, not the proof itself.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def _rosettamath():
    for path in (os.environ.get("ROSETTAMATH_DIR"),
                 os.path.join(ROOT, "..", "RosettaMath"),
                 os.path.expanduser("~/RosettaMath")):
        if path and os.path.isfile(os.path.join(path, "hoare.py")):
            return os.path.abspath(path)
    return None


ROSETTAMATH = _rosettamath()
LEAN = shutil.which("lean") or next(
    (p for p in (os.path.expanduser("~/.local/lean/bin/lean"),
                 os.path.expanduser("~/.elan/bin/lean"))
     if os.path.exists(p)), None)


def _in_big_stack(work):
    out, err = [], []

    def target():
        sys.setrecursionlimit(300000)
        try:
            out.append(work())
        except BaseException as exc:
            err.append(exc)

    threading.stack_size(512 * 1024 * 1024)
    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if err:
        raise err[0]
    return out[0]


def il_of(code, path="<mem>"):
    """The front end, up to IL, on one translation unit."""
    from shivyc import lexer, preproc
    from shivyc.parser.parser import parse
    from shivyc.il_gen import ILCode, SymbolTable, Context
    from shivyc.errors import error_collector
    import shivyc.extensions as extensions
    import shivyc.crust as crust
    import shivyc.contracts as contracts
    code = crust.translate(code, path=path)
    code, ext = extensions.preprocess_extensions(code)
    toks = preproc.process(lexer.tokenize(code, path), path)
    root = parse(toks)
    if not error_collector.ok():
        raise RuntimeError([str(i) for i in error_collector.issues])
    il, table = ILCode(), SymbolTable()
    contracts.reset_unit()
    contracts.install_contracts(getattr(ext, "contracts", None))
    root.make_il(il, table, Context())
    if not error_collector.ok():
        raise RuntimeError([str(i) for i in error_collector.issues])
    return il, table


# Five shapes, each chosen for one thing the lift has to get right.
EXAMPLES = """
int sum_to(int n) { int s = 0; for (int i = 0; i < n; i++) s = s + i; return s; }
int clamp(int x, int hi) { if (x > hi) { x = hi; } else { x = x + 0; } return x; }
int guard(int x) { if (x == 0) return 7; return x; }
int noelse(int x) { if (x > 3) x = 3; return x; }
int nested(int n) { int s = 0; for (int i = 0; i < n; i++) { if (i > 2) s = s + i; else s = s + 1; } return s; }
int shifted(int x) { return x >> 3; }
int divided(int x, int d) { return x / d; }
int modded(int x, int d) { return x % d; }
"""
# Non-negative only, and no zero divisor: the lift is over Nat, and C says
# nothing about either case.  Exercising them here would be testing the
# distance rather than the agreement.
INPUTS = {
    "sum_to": [(0,), (1,), (5,)],
    "clamp": [(3, 9), (9, 3), (4, 4)],
    "guard": [(0,), (5,)],
    "noelse": [(1,), (8,)],
    "nested": [(0,), (3,), (6,)],
    "shifted": [(0,), (7,), (8,), (100,)],
    "divided": [(0, 3), (7, 2), (12, 4), (5, 9)],
    "modded": [(0, 3), (7, 2), (12, 4), (5, 9)],
}


def _nat(term, env, lean4):
    count, current = 0, lean4.normalize(term, env)
    while isinstance(current, lean4.App):
        head, args = lean4.spine(current)
        if not (isinstance(head, lean4.Var) and head.name == "succ"):
            return None
        count += 1
        current = lean4.normalize(args[0], env)
    if isinstance(current, lean4.Var) and current.name == "zero":
        return count
    # A kernel with native Nat literals normalises a closed numeral above
    # zero to one node, possibly under the `succ`s already counted. Read
    # through `getattr` so this works against a kernel without them too.
    lit = getattr(lean4, "NatLit", None)
    if lit is not None and isinstance(current, lit):
        return count + current.value
    return None


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestLiftAgreesWithBinary(unittest.TestCase):
    """Question 1: is the lifted model the function?"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROSETTAMATH)
        d = tempfile.mkdtemp()
        main = ["#include <stdio.h>", EXAMPLES, "int main() {"]
        for fn, cases in INPUTS.items():
            for args in cases:
                main.append('printf("%s %s %%d\\n", %s(%s));'
                            % (fn, " ".join(map(str, args)), fn,
                               ",".join(map(str, args))))
        main.append("return 0; }")
        with open(os.path.join(d, "t.c"), "w") as fh:
            fh.write("\n".join(main))
        proc = subprocess.run(
            [sys.executable, "-m", "shivyc.main", "--no-cache",
             os.path.join(d, "t.c"), "-o", os.path.join(d, "t")],
            capture_output=True, text=True, cwd=ROOT)
        assert proc.returncode == 0, proc.stderr[-800:]
        cls.binary = {}
        run = subprocess.run([os.path.join(d, "t")], capture_output=True,
                             text=True)
        for line in run.stdout.splitlines():
            parts = line.split()
            cls.binary[(parts[0], tuple(map(int, parts[1:-1])))] = int(parts[-1])
        cls.il, cls.table = il_of(EXAMPLES)

    def test_every_example_lifts(self):
        from shivyc.ilproof import lift
        for fn in INPUTS:
            with self.subTest(fn=fn):
                self.assertIn("def %s(" % fn, lift(self.il, self.table, fn))

    def test_lifted_model_agrees_with_the_binary(self):
        from shivyc.ilproof import lift
        import hoare
        import lean4

        def work():
            env = hoare.prelude()
            results = {}
            for fn, cases in INPUTS.items():
                proc = hoare.read_procedure(lift(self.il, self.table, fn),
                                            env, None, ["result == result"])
                for args in cases:
                    term = hoare.app(proc.fn_term,
                                     *[lean4.numeral(a) for a in args])
                    results[(fn, args)] = _nat(term, env, lean4)
            return results

        model = _in_big_stack(work)
        for key, expected in self.binary.items():
            with self.subTest(fn=key[0], args=key[1]):
                self.assertEqual(
                    model[key], expected,
                    "the lifted model and the compiled binary disagree "
                    "about %s%s. One of them is wrong and it is not safe "
                    "to guess which." % key)


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestLiftedLoopTerminates(unittest.TestCase):
    """Question 2, for a loop: the counted idiom's derived annotations are
    enough for the kernel to prove it finishes, for every bound."""

    def test_sum_to_terminates_for_every_n(self):
        sys.path.insert(0, ROSETTAMATH)
        from shivyc.ilproof import lift
        import hoare
        from lean4 import Var

        def work():
            env = hoare.prelude()
            il, table = il_of(EXAMPLES)
            proc = hoare.read_procedure(lift(il, table, "sum_to"), env, None,
                                        ["result == result"])
            goals = dict(proc.loop_obligations)
            carried = proc.shapes[0]["carried"]
            entry = hoare.discharge(goals["invariant holds on entry"], env,
                                    verbose=False)
            kept = hoare.by_cases(env, None, goals["invariant is preserved"],
                                  what="preservation", names=carried)
            down = hoare.by_cases(
                env, None, goals["variant decreases"], what="the variant",
                names=carried,
                using=lambda f, h, g: hoare.app("sub_lt", Var("n"), f["i"],
                                                h[1]))
            return hoare.progress_by_loop(env, proc, entry, kept, down,
                                          verbose=False)

        self.assertIsNotNone(_in_big_stack(work))


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestRealKernelFunction(unittest.TestCase):
    """`elf_regs_for_class`, from crustos/elf.c, through the real front end.

    The scheduler sizes its register save/restore from this function, so a
    bound on it is a bound on that.  CRUSTOS.md says class 3 is 23 registers;
    this proves no class asks for more, for every `cls` including the ones
    the enum does not name.
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROSETTAMATH)
        from shivyc.ilproof import lift
        kernel = os.path.join(ROOT, "crustos", "kernel.c")
        with open(kernel) as fh:
            source = fh.read()
        cwd = os.getcwd()
        os.chdir(os.path.dirname(kernel))    # for the `#include "elf.c"`
        try:
            il, table = il_of(source, path=kernel)
        finally:
            os.chdir(cwd)
        cls.source = lift(il, table, "elf_regs_for_class")
        cls.total = len(il.commands)
        from shivyc.ilproof import LiftError
        cls.lifted = []
        for fn in il.commands:
            try:
                lift(il, table, fn)
                cls.lifted.append(fn)
            except LiftError:
                pass

    def test_it_lifts_to_what_a_person_would_write(self):
        self.assertEqual(self.source.strip(), """
def elf_regs_for_class(cls: 'Nat') -> 'Nat':
    if (cls == 0):
        return 6
    if (cls == 1):
        return 10
    if (cls == 2):
        return 15
    return 23""".strip())

    def test_how_much_of_the_kernel_lifts(self):
        """A number to watch, not a bar to clear: how many of the kernel's
        functions the lift reaches today.  It goes up as the lift learns
        memory access, and this fails if it ever goes down."""
        self.assertGreaterEqual(len(self.lifted), 8,
                                "fewer kernel functions lift than before: %s"
                                % sorted(self.lifted))
        self.assertIn("elf_regs_for_class", self.lifted)

    def test_bound_is_proved_for_every_class(self):
        import hoare

        def work():
            env = hoare.prelude()
            proc = hoare.read_procedure(self.source, env, None,
                                        ["result <= 23"])
            proof = hoare.by_every_bool(env, proc.obligation,
                                        unfolding={"elf_regs_for_class"})
            wrong = hoare.read_procedure(self.source, env, None,
                                         ["result <= 15"])
            try:
                hoare.by_every_bool(env, wrong.obligation,
                                    unfolding={"elf_regs_for_class"})
                refused = False
            except hoare.TheoremError:
                refused = True
            return proof is not None, refused

        proved, refused = _in_big_stack(work)
        self.assertTrue(proved, "result <= 23 should be provable")
        self.assertTrue(refused, "result <= 15 is false for class 3 and "
                                 "should have been refused")

    @unittest.skipUnless(LEAN, "no lean on PATH; run 'make install_lean'")
    def test_lean_accepts_the_bound(self):
        import hoare
        import lean4
        import leanexport

        def work():
            env = hoare.prelude()
            proc = hoare.read_procedure(self.source, env, None,
                                        ["result <= 23"])
            proof = hoare.by_every_bool(env, proc.obligation,
                                        unfolding={"elf_regs_for_class"})
            lean4.define(env, "elf_regs_bounded", proc.obligation, proof)
            slow = hoare.prelude(fast=False)
            values = {n: lean4.value_of(slow, n) for n in hoare.ACCELERATED}
            src, _ = leanexport.export(env, ["elf_regs_bounded"], values,
                                       ["#print axioms elf_regs_bounded"])
            return src

        src = _in_big_stack(work)
        d = tempfile.mkdtemp()
        path = os.path.join(d, "ElfRegs.lean")
        with open(path, "w") as fh:
            fh.write(src)
        run = subprocess.run([LEAN, path], capture_output=True, text=True,
                             timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("does not depend on any axioms", run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
