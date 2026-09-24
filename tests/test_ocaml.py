"""OCaml in Crust: the front end (`tools/ocaml.py`) and OCaml definitions
as proofs (`tools/ocamlproof.py`).

The front end is held to OCaml's own answers -- the types it infers are the
ones `ocaml` prints -- and to refusing, with a line, what the subset does not
have.  The proof backend is held to Curry-Howard both ways: each example in
`examples/ocaml/` is checked by `lean4.py` and then by Lean 4, and each way
OCaml can inhabit a type without proving it is refused.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import ocaml as O                                          # noqa: E402
from tests.test_rustproof import ROSETTAMATH, LEAN, _in_big_stack  # noqa

EXAMPLES = os.path.join(ROOT, "examples", "ocaml")


def _types(code):
    _, c = O.check(code)
    return {name: O.show(s.body) for name, s, _, _ in c.toplevel}


class TestTypes(unittest.TestCase):
    """Types as OCaml infers them."""

    CASES = {
        "let rec map f l = match l with [] -> [] | x :: xs -> f x :: map f xs":
            ("map", "('a -> 'b) -> 'a list -> 'b list"),
        "let rec fold f acc = function [] -> acc | x :: xs -> fold f (f acc x) xs":
            ("fold", "('a -> 'b -> 'a) -> 'a -> 'b list -> 'a"),
        "let rec fact n = if n <= 1 then 1 else n * fact (n - 1)":
            ("fact", "int -> int"),
        "type 'a tree = Leaf | Node of 'a tree * 'a * 'a tree\n"
        "let rec size t = match t with Leaf -> 0 | Node (l, _, r) -> "
        "size l + 1 + size r":
            ("size", "'a tree -> int"),
        "let pair = let id x = x in (id 1, id true)":
            ("pair", "int * bool"),
        "let sign n = match n with 0 -> 0 | n when n > 0 -> 1 | _ -> -1":
            ("sign", "int -> int"),
        "let rec even n = if n = 0 then true else odd (n - 1)\n"
        "and odd n = if n = 0 then false else even (n - 1)":
            ("odd", "int -> bool"),
        "let xs = [1; 2; 3]": ("xs", "int list"),
        "let compose f g x = f (g x)":
            ("compose", "('a -> 'b) -> ('c -> 'a) -> 'c -> 'b"),
        "type t = P of int * int\nlet f (P (a, b)) = a + b": ("f", "t -> int"),
        "type t = P of (int * int)\nlet f (P p) = fst p": ("f", "t -> int"),
        "(* a (* nested *) comment *) let one = 1": ("one", "int"),
        "let swap (a, b) = (b, a)": ("swap", "'a * 'b -> 'b * 'a"),
    }

    def test_inferred_types(self):
        for code, (name, want) in self.CASES.items():
            with self.subTest(name=name):
                self.assertEqual(_types(code)[name], want)

    def test_the_sketch_parses_and_types(self):
        with open(os.path.join(EXAMPLES, "curry_howard.ml")) as fh:
            got = _types(fh.read())
        self.assertEqual(got["modus_ponens_or"],
                         "('a, 'b) or_type -> ('a -> 'c) -> ('b -> 'c) -> 'c")
        self.assertEqual(got["explode"], "void -> 'a")


class TestRefused(unittest.TestCase):
    """Each with the line and a reason; nothing is silently accepted."""

    CASES = {
        "let f x = x + true": "expected int, found bool",
        "let f x = x x": "would have to contain itself",
        "let bad (x : 'a) : 'b = x": "may differ",
        "let bad (x : 'a) : int = x": "only works for int",
        "type t = A | B | C\nlet f x = match x with A -> 1 | B -> 2":
            "C is not matched",
        "let f l = match l with [] -> 0 | _ :: [] -> 1": "not exhaustive",
        "let f (x : int) : 'a = match x with _ -> .": "can match it",
        "let f x = y": "unbound value `y`",
        "type r = { a : int }": "records",
        "let f x = raise x": "exceptions",
        "let r = ref 0": "references",
        'let s = "hi"': "strings",
        "let x = 1.5": "floats",
        "type t = A | B\nlet f x = match x with A | B -> 1": "or-patterns",
        "let x = List.length": "modules",
        "let r = (fun x -> x) (fun y -> y)\nlet a = r 1\nlet b = r true":
            "expected bool, found int",
        "(* never closed": "unterminated comment",
    }

    def test_refusals(self):
        for code, why in self.CASES.items():
            with self.subTest(code=code):
                with self.assertRaises(O.CompileError) as cm:
                    O.check(code)
                self.assertIn(why, str(cm.exception))
                self.assertIsNotNone(cm.exception.line)

    def test_the_line_is_the_offending_one(self):
        with self.assertRaises(O.CompileError) as cm:
            O.check("let a = 1\n\nlet b = a + true\n")
        self.assertEqual(cm.exception.line, 3)


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestProofs(unittest.TestCase):
    """OCaml definitions as proofs, checked by lean4.py and by Lean."""

    @classmethod
    def setUpClass(cls):
        import ocamlproof
        cls.P = ocamlproof

    def _prove(self, path):
        with open(os.path.join(EXAMPLES, path)) as fh:
            return _in_big_stack(lambda: self.P.prove(fh.read()))

    def test_the_sketch_is_five_theorems(self):
        low = self._prove("curry_howard.ml")
        self.assertEqual([n for n, _ in low.theorems],
                         ["theorem1", "theorem2", "modus_ponens_or",
                          "explode", "non_contradiction"])
        stmt = dict(low.theorems)["explode"]
        self.assertEqual(low.H.readable(stmt), "(∀ A : Type 0, (void → A))")

    def test_propositional_logic(self):
        low = self._prove("logic.ml")
        self.assertIn("de_morgan", dict(low.theorems))
        self.assertIn("absurd_left", dict(low.theorems))

    def test_what_is_not_a_proof(self):
        cases = {
            "let rec anything (x : 'a) : 'b = anything x": "prove anything",
            "let three = 3": "not a proof",
            "let pick (b : bool) (x : 'a) : 'a = if b then x else x":
                "not a proposition",
            "let f (l : 'a list) : 'a list = l": "not a proposition",
            "type t = A | B\nlet g (x : t) : t = match x with "
            "A when true -> B | _ -> A": "guards",
            "type 'a t = A of 'a\nlet f (x : 'a t t) : 'a = "
            "match x with A (A y) -> y": "nested",
        }
        for code, why in cases.items():
            with self.subTest(code=code):
                with self.assertRaises((O.CompileError,
                                        self.P.ProofError)) as cm:
                    _in_big_stack(lambda: self.P.prove(code))
                self.assertIn(why, str(cm.exception))

    def test_a_statement_is_what_was_written(self):
        # a proof of `'a -> 'a` is not accepted as one of `'a -> 'b`
        with self.assertRaises(O.CompileError):
            self.P.prove("let wrong (x : 'a) : 'b = x")

    @unittest.skipUnless(LEAN, "lean not on PATH")
    def test_lean_agrees(self):
        for path in ("curry_howard.ml", "logic.ml"):
            low = self._prove(path)
            ok, free, out = _in_big_stack(lambda: self.P.lean_check(low))
            with self.subTest(path=path):
                self.assertTrue(ok, out[-2000:])
                self.assertEqual(sorted(free),
                                 sorted(n for n, _ in low.theorems))


if __name__ == "__main__":
    unittest.main()
