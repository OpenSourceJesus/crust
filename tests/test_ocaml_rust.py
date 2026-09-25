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
