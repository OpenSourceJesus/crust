"""The Rust source lift, checked the ways `test_ilproof.py` checks the IL lift.

`shivyc/rustproof.py` turns a Rust function, contracts included, into the
fragment `hoare.py` proves things about.  Three questions, tested separately
because they fail separately:

  1. Does it lift what it claims, to what a person would write, and refuse
     the rest by name?  (No kernel needed.)

  2. Is the lifted model the function?  Each function is compiled by Crust
     and run, and its lifted term is evaluated through the proof kernel on
     the same inputs.  A disagreement is a bug in the lift.

  3. Can the kernel prove the contract written on the function?  For
     `leanos/regs.rs` -- `elf_regs_for_class` as Rust writes it, a `match`
     -- the `ensures` is proved for every class, a false bound is refused,
     the model agrees with the C version `ilproof.py` lifts from
     `crustos/kernel.c`, and Lean 4 accepts the export.

Questions 2 and 3 are skipped without RosettaMath, as the model tests are;
the Lean check is skipped separately when `lean` is not on PATH.
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from shivyc.rustproof import (LiftError, lift, lift_all,  # noqa: E402
                              signatures, in_dependency_order)
from tests.test_ilproof import (ROSETTAMATH, LEAN, _in_big_stack,  # noqa
                                _nat, il_of)

REGS = os.path.join(ROOT, "leanos", "regs.rs")


def _source(path):
    with open(path) as fh:
        return fh.read()


def _compile_and_print(source, calls):
    """Compile `source` plus a `main` that prints each call on its own line;
    return the printed values."""
    main = "fn main() {\n%s}\n" % "".join(
        '    println!("{}", %s);\n' % c for c in calls)
    workdir = tempfile.mkdtemp()
    src = os.path.join(workdir, "prog.rs")
    out = os.path.join(workdir, "prog")
    with open(src, "w") as fh:
        fh.write(source + "\n" + main)
    proc = subprocess.run(["python3", "-m", "shivyc.main", src, "-o", out],
                          cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    run = subprocess.run([out], capture_output=True, text=True, timeout=60)
    if run.returncode != 0:
        raise AssertionError("the program failed: %s" % run.stderr)
    return [int(x) for x in run.stdout.split()]


def _read_all(lifted, env, hoare):
    """Hand a lifted function and its callees to `read_procedure`, callees
    first; return {name: procedure}."""
    types = {"Nat": hoare.NAT, "Bool": hoare.BOOL}
    procs = {}
    for f in in_dependency_order(lifted):
        sig = {k: ([types[a] for a in v[0]], types[v[1]])
               for k, v in signatures(f).items()}
        ensures = f.ensures or (["result or not result"] if f.ret == "Bool"
                                else ["result == result"])
        procs[f.name] = hoare.read_procedure(f.source, env, sig, ensures)
    return procs


# --------------------------------------------------------------------------
# 1. The lift itself.
# --------------------------------------------------------------------------

class TestLiftShapes(unittest.TestCase):

    def test_elf_regs_lifts_to_what_a_person_would_write(self):
        f = lift(_source(REGS), "elf_regs_for_class")
        self.assertEqual(f.source.strip(), """
def elf_regs_for_class(cls: 'Nat') -> 'Nat':
    if (cls == 0):
        return 6
    elif (cls == 1):
        return 10
    elif (cls == 2):
        return 15
    return 23""".strip())
        self.assertEqual(f.ensures, ["(result <= 23)", "(result >= 6)"])

    def test_every_function_in_regs_rs_lifts(self):
        # Coverage: a function added to the file that does not lift is a
        # failure here rather than silence.
        ok, refused = lift_all(_source(REGS))
        self.assertEqual(refused, {})
        self.assertEqual(sorted(ok), ["class_covers", "class_for_regs",
                                      "elf_regs_for_class"])

    def test_requires_is_a_leading_assert(self):
        f = lift(_source(REGS), "class_covers")
        self.assertTrue(f.source.splitlines()[1].strip().startswith(
            "assert (n <= 23)"))
        self.assertEqual(f.ensures, ["result"])
        self.assertEqual(sorted(c.name for c in f.callees),
                         ["class_for_regs", "elf_regs_for_class"])

    def test_clauses(self):
        f = lift("""
#[requires(flag ==> x > 5)]
#[ensures(result == old(x) + 1)]
#[ensures(result != 0)]
fn f(flag: bool, x: u32) -> u32 { x + 1 }
""", "f")
        self.assertIn("assert ((not flag) or (x > 5))", f.source)
        self.assertEqual(f.ensures, ["(result == (x + 1))",
                                     "(not (result == 0))"])

    def test_match_patterns(self):
        f = lift("""
fn classify(x: u32) -> u32 {
    match x { 0 => 0, 1 | 2 => 1, 3..=9 => 2, n if n % 2 == 0 => 3, _ => 4 }
}
""", "classify")
        self.assertIn("elif ((x == 1) or (x == 2)):", f.source)
        self.assertIn("elif (3 <= x and x <= 9):", f.source)
        # the binding stands for the scrutinee in its guard
        self.assertIn("elif ((x % 2) == 0):", f.source)
        self.assertTrue(f.source.rstrip().endswith("return 4"))

    def test_loops(self):
        f = lift("""
fn between(a: u32, b: u32) -> u32 {
    let mut s: u32 = 0;
    for i in a..=b { s += i; }
    let mut k: u32 = s;
    #[invariant(k <= s)]
    #[variant(k)]
    while k > 1 { k = k / 2; }
    k
}
""", "between")
        self.assertIn("for k in range(((b + 1) - a)):", f.source)
        self.assertIn("i = (a + k)", f.source)
        self.assertIn("assert invariant((k_2 <= s))", f.source)
        self.assertIn("assert variant(k_2)", f.source)

    def test_braced_values_and_nested_locals(self):
        f = lift("""
fn pick(x: u32) -> u32 {
    let y: u32 = if x > 5 { x - 5 } else { 0 };
    if y < 3 { let t: u32 = y * 2; t + 1 } else { y }
}
""", "pick")
        # `t` is first bound inside a branch, so it gets a value up front
        lines = [ln.strip() for ln in f.source.splitlines()]
        self.assertLess(lines.index("t = 0"), lines.index("if (y < 3):"))

    def test_refusals_name_the_construct(self):
        cases = {
            "fn f(x: i32) -> i32 { x }": "signed",
            "fn f(x: &u32) -> u32 { *x }": "by reference",
            "fn f(x: u32) -> u32 { x.count_ones() }": "methods",
            "fn f(x: u32) -> u32 { loop { return x; } }": "`loop`",
            "fn f(n: u32) -> u32 { if n == 0 { 1 } else { f(n - 1) } }":
                "recursive",
            "fn f(x: u32) -> u32 { assert!(x > 0); x }": "macro",
            "fn f(x: u32) -> u8 { x as u8 }": "narrowing",
            "fn f(x: u32) -> u32 { x & 1 }": "bitwise",
            "fn f(x: u32) { }": "nothing to claim",
            "fn f(x: u32) -> u32 { let mut k: u32 = x; "
            "while k > 0 { k = k - 1; } k }": "#[variant",
            "#[ensures(x > 0)] fn f(mut x: u32) -> u32 { x = 1; x }":
                "old(",
            "#[requires(forall(|i: u32| i < x))] fn f(x: u32) -> u32 { x }":
                "quantified",
        }
        for src, word in cases.items():
            with self.subTest(src=src):
                with self.assertRaises(LiftError) as cm:
                    lift(src, "f")
                self.assertIn(word, str(cm.exception))


# --------------------------------------------------------------------------
# 2. The model is the function.
# --------------------------------------------------------------------------

SHAPES = """
fn clamp(x: u32, hi: u32) -> u32 { if x > hi { hi } else { x } }
fn guard(x: u32) -> u32 { if x == 0 { return 7; } x }
fn sum_to(n: u32) -> u32 { let mut s: u32 = 0; for i in 0..n { s += i; } s }
fn between(a: u32, b: u32) -> u32 { let mut s: u32 = 0; for i in a..=b { s = s + i; } s }
fn halve_down(n: u32) -> u32 {
    let mut k: u32 = n;
    #[invariant(k <= n)]
    #[variant(k)]
    while k > 1 { k = k / 2; }
    k
}
fn sq(x: u32) -> u32 { x * x }
fn sq_plus(x: u32) -> u32 { sq(x) + 1 }
fn both(a: u32, b: u32) -> bool { a > 1 && !(b == 0) || a == b }
fn both_n(a: u32, b: u32) -> u32 { both(a, b) as u32 }
fn classify(x: u32) -> u32 { match x { 0 => 0, 1 | 2 => 1, 3..=9 => 2, n if n % 2 == 0 => 3, _ => 4 } }
fn pick(x: u32) -> u32 { let y: u32 = if x > 5 { x - 5 } else { 0 }; let z = match y { 0 => 10, _ => y }; z + 1 }
fn nested(x: u32) -> u32 { if x < 10 { match x { 0 => 1, _ => { let t: u32 = x * 2; t + 1 } } } else if x < 20 { 2 } else { 3 } }
fn shifted(x: u32) -> u32 { (x >> 2) + (x << 1) }
fn widen(x: u8) -> u64 { (x as u32 as u64) + (x >> 1) as u64 }
fn neq_n(a: u32, b: u32) -> u32 { if a != b { 1 } else { 0 } }
"""

INPUTS = {
    "clamp": [(3, 9), (9, 3), (4, 4)],
    "guard": [(0,), (5,)],
    "sum_to": [(0,), (1,), (6,)],
    "between": [(2, 5), (5, 2), (4, 4), (0, 3)],
    "halve_down": [(0,), (1,), (9,), (16,)],
    "sq_plus": [(0,), (3,), (7,)],
    "both_n": [(0, 0), (2, 0), (2, 3), (1, 1), (0, 5)],
    "classify": [(0,), (2,), (5,), (12,), (13,)],
    "pick": [(0,), (5,), (6,), (9,)],
    "nested": [(0,), (4,), (15,), (25,)],
    "shifted": [(0,), (7,), (13,)],
    "widen": [(0,), (9,), (30,)],
    "neq_n": [(3, 3), (3, 4)],
}


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestLiftedModelAgreesWithBinary(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROSETTAMATH)
        calls, keys = [], []
        for fn, cases in INPUTS.items():
            for args in cases:
                calls.append("%s(%s)" % (fn, ", ".join(map(str, args))))
                keys.append((fn, args))
        cls.binary = dict(zip(keys, _compile_and_print(SHAPES, calls)))

    def test_every_shape_lifts(self):
        ok, refused = lift_all(SHAPES)
        self.assertEqual(refused, {})

    def test_lifted_model_agrees_with_the_binary(self):
        import hoare
        import lean4

        def work():
            env = hoare.prelude()
            ok, _ = lift_all(SHAPES)
            procs = {}
            for fn in INPUTS:
                for name, proc in _read_all(ok[fn], env, hoare).items():
                    procs.setdefault(name, proc)
            model = {}
            for fn, cases in INPUTS.items():
                for args in cases:
                    term = hoare.app(procs[fn].fn_term,
                                     *[lean4.numeral(a) for a in args])
                    model[(fn, args)] = _nat(term, env, lean4)
            return model

        model = _in_big_stack(work)
        for key, expected in self.binary.items():
            with self.subTest(fn=key[0], args=key[1]):
                self.assertEqual(
                    model[key], expected,
                    "the lifted model and the compiled binary disagree "
                    "about %s%s. One of them is wrong and it is not safe "
                    "to guess which." % key)


# --------------------------------------------------------------------------
# 3. The contracts on leanos/regs.rs, proved.
# --------------------------------------------------------------------------

@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestRegsContracts(unittest.TestCase):
    """`#[ensures]` on the Rust `elf_regs_for_class`, proved for every class.

    The kernel proves a universally quantified statement -- `forall cls,
    elf_regs_for_class cls <= 23` -- by splitting on each guard of the lifted
    `match` and computing each branch, which is the same proof the C
    version gets through the IL lift.  The difference is where the claim and
    the model come from: both are read off the Rust file that ships.
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROSETTAMATH)
        cls.ok, _ = lift_all(_source(REGS))

    def _prove(self, name, ensures=None):
        import hoare
        lifted = self.ok[name]

        def work():
            env = hoare.prelude()
            procs = _read_all(lifted, env, hoare)
            proc = procs[name]
            if ensures is not None:
                types = {"Nat": hoare.NAT, "Bool": hoare.BOOL}
                sig = {k: ([types[a] for a in v[0]], types[v[1]])
                       for k, v in signatures(lifted).items()}
                proc = hoare.read_procedure(lifted.source, env, sig, ensures)
            try:
                hoare.by_every_bool(env, proc.obligation,
                                    unfolding=set(procs))
                return True
            except hoare.TheoremError:
                return False
        return _in_big_stack(work)

    def test_elf_regs_ensures_are_proved(self):
        self.assertTrue(self._prove("elf_regs_for_class"))

    def test_a_false_bound_is_refused(self):
        self.assertFalse(self._prove("elf_regs_for_class", ["result <= 15"]))

    def test_class_for_regs_ensures_is_proved(self):
        self.assertTrue(self._prove("class_for_regs"))

    def test_class_covers_is_not_yet_proved(self):
        """A known gap, asserted so that closing it cannot go unnoticed.

        `class_covers` composes the two functions, and its postcondition
        needs `n <= 6` to give `6 >= n` -- reasoning about the range a guard
        establishes, which `by_every_bool` (split, then compute) does not
        do.  Until it does, the corpus below stands in: the compiled
        function, with its `ensures` checked at runtime, over every `n` its
        `requires` admits.
        """
        self.assertFalse(self._prove("class_covers"))
        got = _compile_and_print(_source(REGS),
                                 ["class_covers(%d) as u32" % n
                                  for n in range(24)])
        self.assertEqual(got, [1] * 24)

    def test_rust_and_c_versions_agree(self):
        """The Rust model and the C model lifted from `crustos/kernel.c`
        are the same function on every class the kernel names and beyond."""
        import hoare
        import lean4
        from shivyc.ilproof import lift as il_lift
        kernel = os.path.join(ROOT, "crustos", "kernel.c")
        cwd = os.getcwd()
        os.chdir(os.path.dirname(kernel))
        try:
            il, table = il_of(_source(kernel), path=kernel)
        finally:
            os.chdir(cwd)
        c_source = il_lift(il, table, "elf_regs_for_class")
        rust = self.ok["elf_regs_for_class"]

        def work():
            env_c, env_r = hoare.prelude(), hoare.prelude()
            c_proc = hoare.read_procedure(c_source, env_c, None,
                                          ["result == result"])
            r_proc = _read_all(rust, env_r, hoare)["elf_regs_for_class"]
            out = []
            for cls in range(8):
                a = _nat(hoare.app(c_proc.fn_term, lean4.numeral(cls)),
                         env_c, lean4)
                b = _nat(hoare.app(r_proc.fn_term, lean4.numeral(cls)),
                         env_r, lean4)
                out.append((cls, a, b))
            return out

        for cls, a, b in _in_big_stack(work):
            with self.subTest(cls=cls):
                self.assertEqual(a, b)

    @unittest.skipUnless(LEAN, "lean not on PATH")
    def test_lean_accepts_the_rust_bound(self):
        import hoare
        import lean4
        import leanexport
        lifted = self.ok["elf_regs_for_class"]

        def work():
            env = hoare.prelude()
            proc = _read_all(lifted, env, hoare)["elf_regs_for_class"]
            proof = hoare.by_every_bool(env, proc.obligation,
                                        unfolding={"elf_regs_for_class"})
            lean4.define(env, "rust_elf_regs_bounded", proc.obligation, proof)
            slow = hoare.prelude(fast=False)
            values = {n: lean4.value_of(slow, n) for n in hoare.ACCELERATED}
            src, _ = leanexport.export(
                env, ["rust_elf_regs_bounded"], values,
                ["#print axioms rust_elf_regs_bounded"])
            return src

        src = _in_big_stack(work)
        path = os.path.join(tempfile.mkdtemp(), "RustElfRegs.lean")
        with open(path, "w") as fh:
            fh.write(src)
        run = subprocess.run([LEAN, path], capture_output=True, text=True,
                             timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("does not depend on any axioms", run.stdout)


# --------------------------------------------------------------------------
# 4. The safety lift: every place the Rust could panic, as a claim.
# --------------------------------------------------------------------------

sys.path.insert(0, os.path.join(ROOT, "tools"))
ALLOC = os.path.join(ROOT, "leanos", "alloc.rs")

SAFETY = """
fn guarded_sub(x: u32) -> u32 { if x >= 5 { x - 5 } else { 0 } }
fn strict_sub(x: u32) -> u32 { if x > 5 { x - 5 } else { 0 } }
fn early_sub(x: u32, y: u32) -> u32 { if x < y { return 0; } x - y }
fn raw_sub(x: u32, y: u32) -> u32 { x - y }
fn div(a: u32, b: u32) -> u32 { if b == 0 { return 0; } a / b }
fn first(xs: &[u32]) -> u32 { if xs.len() > 0 { xs[0] } else { 0 } }
fn at(xs: &[u32], i: usize) -> u32 { if i >= xs.len() { return 0; } xs[i] }
fn short(xs: &[u32], i: usize) -> bool { i < xs.len() && xs[i] > 3 }
fn add(a: u32, b: u32) -> u32 { a + b }
fn raw_index(xs: &[u32], i: usize) -> u32 { xs[i] }
#[requires(n > 0)]
fn pos(n: u32) -> u32 { n }
fn call_ok(n: u32) -> u32 { if n > 0 { pos(n) } else { 0 } }
fn call_bad(n: u32) -> u32 { pos(n) }
#[requires(i < xs.len())]
fn behind_requires(xs: &[u32], i: usize) -> u32 { xs[i] }
"""

# Whether each function's every obligation is proved.  The `False` rows are
# genuinely unsafe -- each has an input that panics -- except `add`, which
# overflows at u32::MAX and is stated against the symbolic maximum.
SAFE = {
    "guarded_sub": True, "strict_sub": True, "early_sub": True,
    "raw_sub": False, "div": True, "first": True, "at": True,
    "short": True, "add": False, "raw_index": False, "call_ok": True,
    "call_bad": False, "behind_requires": True,
}


class TestSafetyLift(unittest.TestCase):
    """The shape of the safety lift, which needs no kernel."""

    def test_obligations_are_named_where_they_arise(self):
        f = lift(SAFETY, "early_sub")
        self.assertEqual(f.obligations, ["`-` may underflow (line 4)"])
        self.assertEqual(lift(SAFETY, "add").obligations,
                         ["`+` may overflow u32 (line 10)"])
        self.assertEqual(lift(SAFETY, "call_bad").obligations,
                         ["`pos`'s `#[requires]` (line 15)"])
        self.assertIsNone(lift(_source(REGS), "elf_regs_for_class").safety())

    def test_an_obligation_is_paid_before_its_statement(self):
        src = lift(SAFETY, "early_sub").safety()
        lines = [ln.strip() for ln in src.splitlines()]
        at = lines.index("if (x < y):")
        self.assertTrue(lines[at + 2].startswith("_ok = (_ok and"))
        self.assertEqual(lines[at + 3], "return _ok")

    def test_overflow_is_stated_against_a_symbolic_maximum(self):
        # The kernel's numerals are unary: u32::MAX written out would be four
        # billion terms.  The maximum is a parameter, and each value of the
        # type is assumed to be at most it.
        src = lift(SAFETY, "add").safety()
        self.assertIn("max_u32: 'Nat'", src.splitlines()[0])
        self.assertIn("assert (a <= max_u32)", src)
        self.assertIn("((a + b) <= max_u32)", src)
        self.assertNotIn("4294967295", src)

    def test_requires_guards_the_body(self):
        src = lift(SAFETY, "behind_requires").safety()
        self.assertIn("if (i < len(xs)):", src)
        self.assertNotIn("assert (i < len(xs))", src)

    def test_a_guarded_obligation_is_conditioned_on_its_path(self):
        src = lift(SAFETY, "short").safety()
        self.assertIn("if (i < len(xs)) else True)", src)


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestSafetyProofs(unittest.TestCase):

    def test_each_function_is_safe_exactly_when_expected(self):
        import rustprove
        prover = rustprove.Prover(SAFETY)
        for name, expected in SAFE.items():
            with self.subTest(fn=name):
                verdicts = prover.safety(name)
                self.assertTrue(verdicts)
                self.assertEqual(all(ok for _, ok in verdicts), expected,
                                 verdicts)


# --------------------------------------------------------------------------
# 5. Records, and a `&mut` parameter as state.
# --------------------------------------------------------------------------

RECORDS = """
struct Counter { n: u32, cap: u32 }

#[requires(c.n < c.cap)]
#[ensures(c.n <= c.cap)]
#[ensures(c.cap == old(c.cap))]
fn step(c: &mut Counter) { c.n += 1; }

#[ensures(result <= c.cap)]
fn room(c: &Counter) -> u32 { if c.n <= c.cap { c.cap - c.n } else { 0 } }
"""


class TestRecordsLift(unittest.TestCase):

    def test_a_mut_parameter_is_threaded_state(self):
        f = lift(RECORDS, "step")
        self.assertEqual(f.source.splitlines()[0],
                         "def step(c: 'Counter') -> 'Counter':")
        self.assertIn("c.n = (c.n + 1)", f.source)
        self.assertTrue(f.source.rstrip().endswith("return c"))
        # the parameter is its final value; `old` is the one it came in with
        self.assertEqual(f.ensures, ["(result.n <= result.cap)",
                                     "(result.cap == c.cap)"])
        self.assertEqual(f.records, [("Counter", [("n", "Nat"),
                                                  ("cap", "Nat")])])


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestRecordsProofs(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import rustprove
        cls.prover = rustprove.Prover(RECORDS)

    def test_room_contract_and_safety(self):
        self.assertTrue(self.prover.contract("room"))
        self.assertTrue(all(ok for _, ok in self.prover.safety("room")))

    def test_the_model_agrees_with_the_binary(self):
        cases = [(3, 9), (9, 9), (12, 9), (0, 0)]
        calls = []
        for n, cap in cases:
            calls.append("room(&Counter { n: %d, cap: %d })" % (n, cap))
        binary = _compile_and_print(RECORDS, calls)
        H, L, p = self.prover.H, self.prover.L, self.prover

        def work():
            env, _ = p.fresh_env(p.lifted["room"])
            proc = H.read_procedure(p.lifted["room"].source, env, None,
                                    ["result == result"])
            return [_nat(H.app(proc.fn_term,
                               H.app("Counter.mk", L.numeral(n),
                                     L.numeral(cap))), env, L)
                    for n, cap in cases]
        self.assertEqual(_in_big_stack(work), binary)

    def test_step_contract_is_not_yet_proved(self):
        """A known gap, asserted so that closing it cannot go unnoticed.

        `step`'s postconditions follow from its precondition through a record
        update read back through a projection -- `Counter.n (with_n c v)` is
        `v` -- and the automation does not yet carry the hypothesis across
        that reduction.  The runtime check stands in: Crust checks both
        `ensures` on every call."""
        self.assertFalse(self.prover.contract("step"))


# --------------------------------------------------------------------------
# 6. leanos/alloc.rs: the allocator, ported, and proved from its source.
# --------------------------------------------------------------------------

# The Python allocator's region layout and corpus, reused so the port is
# checked against the original row for row.
from tests.test_alloc_model import CORPUS, _python, B, S, O  # noqa: E402

# The obligations of alloc.rs the kernel does not settle.  Every one is a
# `u64` addition: it fits whenever the surrounding code admits it, but
# saying so is arithmetic about the maximum, which splitting and computing
# does not do.  A new open obligation fails this test, and so does closing
# one of these, so neither can happen quietly.
ALLOC_OPEN = {
    ("bump", "`+` may overflow u64 (line 58)"),
    ("bump", "`+` may overflow u64 (line 59)"),
    ("slot_addr", "`+` may overflow u64 (line 70)"),
    ("slot_ok", "`+` may overflow u64 (line 78)"),
}


def _alloc_calls(row):
    _, tid, heap, used, n = row
    arrays = "&[%s][..], &[%s][..]" % (", ".join("%du64" % b for b in B),
                                       ", ".join("%du64" % s for s in S))
    owners = "&[%s][..]" % ", ".join("%du32" % o for o in O)
    return [
        "bump(%s, %s, %d, %d, %d, %d)" % (arrays, owners, tid, heap, used, n),
        "if %d < %d { slot_addr(&[%s][..], %d, %d) } else { 0 }"
        % (heap, len(B), ", ".join("%du64" % b for b in B), heap, used),
        "slot_ok(%s, %d, %d)" % (arrays, heap, used),
    ]


class TestAllocLift(unittest.TestCase):

    def test_every_function_lifts(self):
        ok, refused = lift_all(_source(ALLOC))
        self.assertEqual(refused, {})
        self.assertEqual(sorted(ok), ["bump", "contains", "slot_addr",
                                      "slot_ok"])

    def test_the_port_agrees_with_the_python(self):
        calls = []
        for row in CORPUS:
            calls.extend(_alloc_calls(row))
        got = _compile_and_print(_source(ALLOC), calls)
        for k, row in enumerate(CORPUS):
            with self.subTest(case=row[0]):
                self.assertEqual(tuple(got[3 * k:3 * k + 3]), _python(row))


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestAllocProofs(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import rustprove
        cls.prover = rustprove.Prover(_source(ALLOC))

    def test_the_model_agrees_with_the_binary(self):
        calls = []
        for row in CORPUS:
            calls.extend(_alloc_calls(row)[:1])
        binary = _compile_and_print(_source(ALLOC), calls)
        H, L, p = self.prover.H, self.prover.L, self.prover

        def work():
            env, _ = p.fresh_env(p.lifted["bump"])
            proc = H.read_procedure(p.lifted["bump"].source, env, None,
                                    ["result == result"])
            out = []
            for _, tid, heap, used, n in CORPUS:
                term = H.app(proc.fn_term, H.array(B), H.array(S), H.array(O),
                             L.numeral(tid), L.numeral(heap),
                             L.numeral(used), L.numeral(n))
                out.append(_nat(term, env, L))
            return out
        self.assertEqual(_in_big_stack(work), binary)

    def test_contracts_are_proved(self):
        # `bump`'s `used <= result` is alloc_eq.py's `bump_monotone`
        self.assertTrue(self.prover.contract("bump"))
        self.assertTrue(self.prover.contract("slot_ok"))

    def test_bump_bounded(self):
        """alloc_eq.py's `bump_bounded`, proved the way alloc_eq.py proves
        it -- but about the model lifted from alloc.rs, not a hand copy."""
        H, L, p = self.prover.H, self.prover.L, self.prover
        lifted = p.lifted["bump"]

        def work():
            env, sig = p.fresh_env(lifted)
            H.read_procedure(lifted.source, env, sig, ["result == result"])
            V = L.Var
            size = H.app("nth", H.NAT, L.numeral(0), V("sizes"), V("heap"))
            bumped = H.app("bump", V("bases"), V("sizes"), V("owners"),
                           V("tid"), V("heap"), V("used"), V("n"))
            opened = H.unfold(bumped, env, {"bump"})
            hyp = H.app("Holds", H.app("leb", V("used"), size))
            goal = H.app("Holds", H.app("leb", opened, size))
            proof = H.bound_by_ites_or_guards(env, goal, V("h"), hyp)
            stmt = H.arrow(hyp, H.app("Holds", H.app("leb", bumped, size)))
            term = L.Lambda("h", hyp, proof)
            params = [("bases", H.BYTES), ("sizes", H.BYTES),
                      ("owners", H.BYTES), ("tid", H.NAT), ("heap", H.NAT),
                      ("used", H.NAT), ("n", H.NAT)]
            for name, ty in reversed(params):
                stmt, term = L.Pi(name, ty, stmt), L.Lambda(name, ty, term)
            H.prove(stmt, term, env, verbose=False)
            return True
        self.assertTrue(_in_big_stack(work))

    def test_only_the_overflow_obligations_are_open(self):
        open_now = set()
        for name in self.prover.lifted:
            for label, ok in self.prover.safety(name):
                if not ok:
                    open_now.add((name, label))
        self.assertEqual(open_now, ALLOC_OPEN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
