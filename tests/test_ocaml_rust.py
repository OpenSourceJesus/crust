"""OCaml compiled through Crust's Rust subset, and the Rust fixes it needed.

`tools/ocaml2rust.py` lowers a typed OCaml program to Rust; Crust compiles
it; the output is compared with `tools/ocamlinterp.py`, a direct evaluator
of the same program, and with the output pinned here.  Two witnesses, so
that a change to either is caught by the other.

The Rust front end needed four fixes to carry OCaml's data, each pinned in
`TestRustFixes` as the smallest Rust that showed it:

  * an enum whose payload is a struct -- a user struct, a `Vec`, a `Box` --
    did not compile: data enums were emitted before every struct;
  * a tuple literal ignored its expected type;
  * a variant's payload was not its constructor's signature;
  * a block that was another block's tail lost its value, and a unit
    `fn main` exited with its last `printf`'s byte count.
"""
import os
import resource
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import ocaml as O                                          # noqa: E402
import ocaml2rust                                          # noqa: E402
import ocamlinterp                                         # noqa: E402

RUN = os.path.join(ROOT, "examples", "ocaml", "run")


def compile_rust(source, name="prog"):
    """Build a Rust source with Crust; the executable's path."""
    d = tempfile.mkdtemp()
    src, exe = os.path.join(d, name + ".rs"), os.path.join(d, name)
    with open(src, "w") as fh:
        fh.write(source)
    r = subprocess.run([sys.executable, "-m", "shivyc.main", src, "-o", exe],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(r.stdout + r.stderr)
    return exe


def run(exe, stack_kb=None):
    def limit():
        if stack_kb is not None:
            resource.setrlimit(resource.RLIMIT_STACK,
                               (stack_kb * 1024, stack_kb * 1024))
    r = subprocess.run([exe], capture_output=True, text=True, timeout=60,
                       preexec_fn=limit)
    return r.returncode, r.stdout


class TestRustFixes(unittest.TestCase):

    def rust(self, code):
        return run(compile_rust(code))

    def test_enum_with_struct_payloads(self):
        self.assertEqual(self.rust(
            'struct P { x: i64 } enum S { A(P), B } '
            'fn f(s: S) -> i64 { match s { S::A(p) => p.x, S::B => 0 } } '
            'fn main() { println!("{}", f(S::A(P { x: 7 }))); }'), (0, "7\n"))
        self.assertEqual(self.rust(
            'enum S { A(Vec<i64>), B } fn f(s: S) -> usize { match s { '
            'S::A(v) => v.len(), S::B => 0 } } fn main() { let mut v = '
            'Vec::<i64>::new(); v.push(1); println!("{}", f(S::A(v))); }'),
            (0, "1\n"))

    def test_recursive_enums(self):
        self.assertEqual(self.rust(
            'enum T { Leaf, Node(Box<T>, i64, Box<T>) } fn size(t: T) -> i64 '
            '{ match t { T::Leaf => 0, T::Node(l, _, r) => size(l.get()) + 1 '
            '+ size(r.get()) } } fn main() { let t = T::Node(Box::<T>::new('
            'T::Leaf), 5, Box::<T>::new(T::Node(Box::<T>::new(T::Leaf), 6, '
            'Box::<T>::new(T::Leaf)))); println!("{}", size(t)); }'),
            (0, "2\n"))

    def test_an_enum_holding_itself_by_value_is_refused(self):
        with self.assertRaises(AssertionError) as cm:
            compile_rust('enum L { Nil, Cons(i64, L) } fn main() { }')
        self.assertIn("put the recursive part in a `Box`", str(cm.exception))

    def test_a_tuple_literal_takes_its_expected_type(self):
        self.assertEqual(self.rust(
            'fn swap(p: (i64, i64)) -> (i64, i64) { let (a, b) = p; (b, a) } '
            'fn main() { let q = swap((1, 2)); println!("{} {}", q.0, q.1); '
            'let n: (i64, (i64, bool)) = (1, (2, true)); '
            'println!("{}", (n.1).0); }'), (0, "2 1\n2\n"))

    def test_a_payload_is_its_constructors_signature(self):
        self.assertEqual(self.rust(
            'enum S { A(Box<i64>), B } fn main() { let s = S::A('
            'Box::<i64>::new(3)); match s { S::A(b) => println!("{}", '
            'b.get()), S::B => println!("b") } }'), (0, "3\n"))

    def test_a_tail_block_keeps_its_value(self):
        for code, want in [
            ('fn f() -> i64 { { let a: i64 = 1; if a == 1 { 5 } else { 6 } }'
             ' } fn main() { println!("{}", f()); }', "5\n"),
            ('fn f() -> i64 { let r: i64 = { let a: i64 = 2; if a == 1 { 5 }'
             ' else { { let c: i64 = 9; if c == 9 { c } else { 0 } } } }; r }'
             ' fn main() { println!("{}", f()); }', "9\n"),
            ('fn f() -> i64 { let mut x: i64 = 1; { x = x + 2; } x } '
             'fn main() { println!("{}", f()); }', "3\n"),
        ]:
            with self.subTest(code=code):
                self.assertEqual(self.rust(code), (0, want))

    def test_a_unit_main_exits_zero(self):
        for code in ['fn main() { println!("x") }',
                     'fn main() { { println!("x") } }',
                     'fn main() { println!("x"); return; }']:
            with self.subTest(code=code):
                self.assertEqual(self.rust(code), (0, "x\n"))


# Each program, and what OCaml prints for it.
EXPECTED = {
    "poly.ml": "321\n571\n2\n12\n",
    "patterns.ml": "129100\n397959\n01234\n1234\n",
    "local.ml": "5050\n18\n01\n11\n",
    "higher.ml": "10\n24\n12\n1112\n1120\n15\n",
    "deep.ml": "5000050000\n10000100000\n",
    "contracts.ml": "1078\n",
    "division.ml": "4-3-1-4611686018427387904\n",
    "recursion.ml": "557\n",
    "tailrec.ml": "1010\n",
    "lists.ml": "39\n",
    "trees.ml": "331\n",
    "mutual.ml": "31\n",
    "loop.ml": "2999998\n111\n",
}


class TestCompiledOCaml(unittest.TestCase):
    """Compiled, and compared with the interpreter and the pinned output."""

    def test_every_example(self):
        for name, want in sorted(EXPECTED.items()):
            with self.subTest(program=name):
                with open(os.path.join(RUN, name)) as fh:
                    code = fh.read()
                self.assertEqual(ocamlinterp.run(code), want)
                rc, got = run(compile_rust(ocaml2rust.lower(code), name[:-3]))
                self.assertEqual((rc, got), (0, want))

    def test_every_example_is_pinned(self):
        self.assertEqual(sorted(f for f in os.listdir(RUN)
                                if f.endswith(".ml")), sorted(EXPECTED))

    def test_a_tail_call_is_a_jump(self):
        """A million steps in a 256 KB stack: as calls, ~100 MB."""
        with open(os.path.join(RUN, "loop.ml")) as fh:
            exe = compile_rust(ocaml2rust.lower(fh.read()), "loop")
        self.assertEqual(run(exe, stack_kb=256), (0, EXPECTED["loop.ml"]))

    def test_integers_are_63_bits(self):
        code = ("let () = print_int (4611686018427387903 + 1); "
                "print_newline (); print_int (-7 / 2); print_int (-7 mod 2); "
                "print_newline (); print_int (4611686018427387903 * 3); "
                "print_newline ()")
        want = "-4611686018427387904\n-3-1\n4611686018427387901\n"
        self.assertEqual(ocamlinterp.run(code), want)
        self.assertEqual(run(compile_rust(ocaml2rust.lower(code))), (0, want))


class TestProvedOCaml(unittest.TestCase):
    """Compiled OCaml under the Rust proof tooling: a contract written as
    `[@@ensures ..]` is proved about the Rust, and each arithmetic
    operation owes staying in OCaml's 63 bits -- which `abs` does not at
    `min_int`, and a `[@@requires x > min_int]` makes it."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, "..", "RosettaMath"))
        import rustprove
        with open(os.path.join(RUN, "contracts.ml")) as fh:
            cls.rust = ocaml2rust.lower(fh.read())
        d = tempfile.mkdtemp()
        path = os.path.join(d, "contracts.rs")
        with open(path, "w") as fh:
            fh.write(cls.rust)
        cls.prover = rustprove.Prover(*rustprove.load_unit(path))

    def verdicts(self, name):
        p = self.prover
        fn = p.lifted[name]
        out = {}
        if fn.ensures:
            out["ensures"] = bool(p.contract(name))
        for label, ok in p.safety(name):
            out[label.split(" (line")[0]] = ok
        return out

    def test_contracts_are_proved(self):
        for name in ("ml_clamp__int", "ml_iabs", "ml_iabs2"):
            with self.subTest(fn=name):
                self.assertTrue(self.verdicts(name)["ensures"])

    def test_abs_wraps_at_min_int(self):
        self.assertEqual(self.verdicts("ml_iabs")
                         ["OCaml `int` `-` may wrap"], False)

    def test_excluding_min_int_proves_it(self):
        self.assertEqual(self.verdicts("ml_iabs2")
                         ["OCaml `int` `-` may wrap"], True)

    def test_lean_agrees(self):
        """Every obligation settled about the compiled OCaml -- the 63-bit
        one included -- is a theorem Lean 4 accepts, with no axioms."""
        import rustlean
        if rustlean.find_lean() is None:
            self.skipTest("lean not installed")
        rustlean.prove_everything(self.prover)
        verdicts = rustlean.check(self.prover.certificates,
                                  tempfile.mkdtemp())
        self.assertGreaterEqual(len(verdicts), 4)
        self.assertTrue(all(v.agreed for v in verdicts),
                        [str(v) for v in verdicts])

    def test_contract_runs_too(self):
        """Crust checks a contract at run time as well: calling clamp
        against its `requires` stops the program."""
        rust = self.rust.replace("ml_clamp__int(0i64, 10i64, 42i64)",
                                 "ml_clamp__int(10i64, 0i64, 42i64)")
        self.assertNotEqual(rust, self.rust)
        rc, _ = run(compile_rust(rust, "broken"))
        self.assertNotEqual(rc, 0)


# What the prover settles about each example, and what it must leave open:
# an open obligation here is a real overflow or wrap, as much a claim as a
# proof.  (function, label prefix) -> proved?
VERDICTS = {
    "division.ml": {
        ("ml_half", "ensures"): True,
        ("ml_half", "`/` by zero"): True,
        ("ml_half", "OCaml `int` `/` may wrap"): True,       # |x/2| <= |x|
        ("ml_safe_div", "`/` by zero"): True,
        ("ml_safe_div", "OCaml `int` `/` may wrap"): False,  # min_int / -1
        ("ml_rem", "`%` by zero"): True,
    },
    "recursion.ml": {
        ("ml_dist", "ensures"): True,
        ("ml_dist", "the recursive call's"): True,
        ("ml_dist", "OCaml `int` `+` may wrap"): True,       # 1 + dist <= n
        ("ml_sum_to", "ensures"): True,
        ("ml_sum_to", "the recursive call's"): True,
        ("ml_sum_to", "OCaml `int` `+` may wrap"): False,    # it does, for
    },                                                        # large n
    "tailrec.ml": {
        ("ml_down", "ensures"): True,
        ("ml_down", "the recursive call's"): True,
        ("ml_down", "OCaml `int` `-` may wrap"): True,
        ("ml_down", "OCaml `int` `+` may wrap"): False,      # acc unbounded
        ("ml_count_up", "`ml_go3`'s `#[requires]`"): True,
        ("ml_go3", "ensures"): True,
        ("ml_go3", "the recursive call's"): True,
    },
    # data: a variant is an inductive type, a `*mut` in it the value it
    # points to (the heap discipline, checked); each projection owes its
    # variant, each unreachable `panic!` owes `False`
    "lists.ml": {
        ("ml_length__int", "ensures"): True,
        ("ml_length__int", "the recursive call's"): True,  # size t < size l
        ("ml_length__int", "`ml_ml_list_int_Cons_1` on another"): True,
        ("ml_length__int", "`panic!` is reached"): True,
        ("ml_sum", "the recursive call's"): True,
        ("ml_sum", "`panic!` is reached"): True,
        ("ml_sum", "OCaml `int` `+` may wrap"): False,       # it can
    },
    "trees.ml": {
        ("ml_size", "ensures"): True,
        ("ml_size", "the recursive call's"): True,          # both subtrees
        ("ml_mirror", "the recursive call's"): True,
        ("ml_all_pos", "the recursive call's"): True,
        ("ml_insert", "ensures"): True,                     # <> Leaf
        ("ml_insert", "`ml_ml_tree_Node_1` on another"): True,
        ("ml_insert", "`panic!` is reached"): True,
    },
    # mutual recursion: `expr` and `stmt` one mutual inductive group in
    # the kernel (and a `mutual` block in Lean); a call between `count_e`
    # and `count_s`, or `even` and `odd`, owes the callee's `requires` and
    # its variant below the caller's -- one induction over the group
    "mutual.ml": {
        ("ml_count_e", "ensures"): True,     # through count_s's contract
        ("ml_count_s", "ensures"): True,
        ("ml_count_e", "the call to `ml_count_s`'s"): True,
        ("ml_count_s", "the call to `ml_count_e`'s"): True,
        ("ml_count_e", "the recursive call's"): True,
        ("ml_count_e", "`panic!` is reached"): True,
        ("ml_count_s", "`panic!` is reached"): True,
        ("ml_count_e", "OCaml `int` `+` may wrap"): False,   # unbounded
        ("ml_even", "the call to `ml_odd`'s"): True,
        ("ml_odd", "the call to `ml_even`'s"): True,
        ("ml_even", "OCaml `int` `-` may wrap"): True,
    },
}


class TestRecursionAndDivision(unittest.TestCase):
    """Signed `/` and `%`, recursion (the contract assumed for smaller
    arguments, each call owing a smaller `[@@variant]`), and tail recursion
    -- which ocaml2rust compiles to a loop, and the lift reads back as the
    recursion it is -- each proved about the compiled Rust."""

    def verdicts(self, prover):
        out = {}
        for name in prover.lifted:
            if name not in prover.own:
                continue
            fn = prover.lifted[name]
            if fn.ensures:
                out[(name, "ensures")] = bool(prover.contract(name))
            for label, ok in prover.safety(name):
                out[(name, label)] = ok
        return out

    def check(self, example):
        sys.path.insert(0, os.path.join(ROOT, "..", "RosettaMath"))
        import rustprove
        with open(os.path.join(RUN, example)) as fh:
            rust = ocaml2rust.lower(fh.read())
        path = os.path.join(tempfile.mkdtemp(), "p.rs")
        with open(path, "w") as fh:
            fh.write(rust)
        prover = rustprove.Prover(*rustprove.load_unit(path))
        got = self.verdicts(prover)
        for (fn, prefix), want in VERDICTS[example].items():
            found = [ok for (f, label), ok in got.items()
                     if f == fn and label.startswith(prefix)]
            with self.subTest(example=example, fn=fn, obligation=prefix):
                self.assertTrue(found, "no such obligation")
                self.assertEqual(all(found), want)
        import rustlean
        if rustlean.find_lean() is not None:
            verdicts = rustlean.check(prover.certificates, tempfile.mkdtemp())
            self.assertTrue(all(v.agreed for v in verdicts),
                            [str(v) for v in verdicts])

    def test_division(self):
        self.check("division.ml")

    def test_recursion(self):
        self.check("recursion.ml")

    def test_tail_recursion(self):
        self.check("tailrec.ml")

    def test_lists(self):
        self.check("lists.ml")

    def test_trees(self):
        self.check("trees.ml")

    def test_the_heap_discipline_is_checked_not_assumed(self):
        """A pointer is read as its value only where nothing could have
        written it since: a changed box, a write through a pointer
        elsewhere, or a `free` and the enum is not lifted at all."""
        import shivyc.rustproof as R
        with open(os.path.join(RUN, "lists.ml")) as fh:
            rust = ocaml2rust.lower(fh.read())
        self.assertIsNone(R._Unit(rust).heap_refusal)
        cases = {
            rust.replace("p[0] = v; p }", "p[0] = v; p[0] = v; p }", 1):
                "not the helper",
            rust + "fn evil(p: *mut ml_list_int) { p[0] = "
                   "ml_list_int::Nil; }\n": "writes through an index",
            rust + "fn evil(p: *mut ml_list_int) { free(p); }\n": "frees",
        }
        for src, why in cases.items():
            with self.subTest(why=why):
                self.assertIn(why, R._Unit(src).heap_refusal)
                with self.assertRaises(R.LiftError):
                    R.lift(src, "ml_length__int")

    def test_mutual(self):
        self.check("mutual.ml")

    def test_mutual_functions_need_variants(self):
        rust = ocaml2rust.lower(
            "let rec f n = if n <= 0 then 0 else g (n - 1)\n"
            "and g n = if n <= 0 then 1 else f (n - 1)\n"
            "let () = print_int (f 3)")
        import shivyc.rustproof as R
        with self.assertRaises(R.LiftError) as cm:
            R.lift(rust, "ml_f")
        self.assertIn("variant", str(cm.exception))

    def test_a_recursive_function_needs_a_variant(self):
        rust = ocaml2rust.lower(
            "let rec f n = if n <= 0 then 0 else f (n - 1)\n"
            "  [@@ensures result = 0]\nlet () = print_int (f 3)")
        import shivyc.rustproof as R
        with self.assertRaises(R.LiftError) as cm:
            R.lift(rust, "ml_f")
        self.assertIn("variant", str(cm.exception))


class TestRefused(unittest.TestCase):
    CASES = {
        "let add a b = a + b\nlet inc = add 1\nlet () = print_int (inc 2)":
            "partial application",
        "let make k = fun x -> x + k\nlet () = print_int ((make 1) 2)":
            "captures k",
        "let () = print_int (if [1] = [1] then 1 else 0)":
            "only ints and bools compare",
        "type t = A of (t * int) | B\nlet f x = match x with B -> 0 | A _ -> 1"
        "\nlet () = print_int (f B)": "holds a variant inside a tuple",
    }

    def test_refusals(self):
        for code, why in self.CASES.items():
            with self.subTest(code=code):
                with self.assertRaises(ocaml2rust.LowerError) as cm:
                    ocaml2rust.lower(code)
                self.assertIn(why, str(cm.exception))


class TestInterpreter(unittest.TestCase):
    def test_semantics(self):
        self.assertEqual(ocamlinterp.run(
            "let rec f n = if n = 0 then 0 else 1 + f (n - 1)\n"
            "let () = print_int (f 200000)"), "200000")
        with self.assertRaises(ocamlinterp.RuntimeFailure):
            ocamlinterp.run("let () = print_int (1 / 0)")


if __name__ == "__main__":
    unittest.main()
