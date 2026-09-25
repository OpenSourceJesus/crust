# OCAML — an OCaml subset for Crust

Three tools over one front end, and a reference:

| Tool | What it does |
|------|--------------|
| `tools/ocaml.py` | lexer, parser, Hindley–Milner type inference; `python3 tools/ocaml.py FILE.ml` prints each top-level name and its type |
| `tools/ocamlproof.py` | each top-level definition as a proof of its type (Curry–Howard), checked by RosettaMath's `lean4.py` and then by Lean 4 |
| `tools/ocaml2rust.py` | an OCaml program as Rust in Crust's subset, compiled to run; see below |
| `tools/ocamlinterp.py` | the same program evaluated directly: the reference the compiled code is checked against |

## The front end (`tools/ocaml.py`)

Grown from the sketch that started it; its AST names are kept. Every node
carries its line, operators parse by precedence, and every expression gets a
type by Algorithm W with let-polymorphism and the value restriction, so a
backend knows what each value *is*.

**In:** variant types (parameterised, recursive, empty: `type void = |`),
aliases; `let`, `let rec`, `let .. and ..`, parameters `x`, `(x : t)`, `()`,
`_`, `(a, b)`, result annotations; integers, `true`/`false`, `()`,
application, constructors (`P (a, b)` for `of int * int` is two arguments,
for `of (int * int)` one tuple, as in OCaml; `P _` matches either), tuples,
lists (`[]`, `::`,
`[a; b]`), `fun`, `function`, `match` with `when` guards, `if`/`else`,
`e1; e2` (with `e1 : unit`), `print_int` and `print_newline`,
`let .. in`, `+ - * / mod = <> < > <= >= && ||`, unary `-`, `not`,
`(e : t)`, `begin .. end`, nested comments, and the refutation arm
`_ -> .` — checked, not trusted: its pattern must reach a type with no
constructors.

**Checked:** types, with OCaml's messages in OCaml's direction ("expected
int, found bool"); exhaustiveness of every `match`, naming a missing case
(`C is not matched`); unbound names.

**Out**, each refused with its line: records, `ref`/`:=`/`!`, exceptions
(`raise`, `try`, `exception`), modules and functors (`List.length` included),
objects, labelled and optional arguments, polymorphic variants, GADTs,
strings, floats, chars, arrays, or-patterns, `if` without `else`, loops,
`lazy`, `assert`.  (List patterns `[a; b]` are in, as `a :: b :: []`.)

**One tightening.** A type variable in an annotation is *rigid*:
`let f (x : 'a) : 'b = x` is refused ("the annotation says 'a and 'b may
differ, but the definition makes them the same"), where OCaml unifies the
two and accepts it. An annotation is a promise about what a definition is
for; for a proof it is the statement. `ocaml.check(code, strict=False)`
gives OCaml's reading.

## OCaml definitions as proofs (`tools/ocamlproof.py`)

| OCaml | Proposition | Kernel |
|-------|-------------|--------|
| `'a` | a proposition | `(A : Type)` |
| `t1 -> t2` | implication | `Pi` |
| `t1 * t2` | conjunction | `Prod`, `mk`, `fst`, `snd` |
| `type ('a,'b) t = L of 'a \| R of 'b` | disjunction | an inductive of its own; `match` is `t.rec` |
| `type void = \|` | falsehood | an inductive with no constructors |
| `match v with _ -> .` | ex falso | `void.rec` with no cases |
| a polymorphic definition | its statement for every proposition | `Lambda` over the type variables |

```ocaml
type ('a, 'b) or_type = Left of 'a | Right of 'b
let modus_ponens_or (disj : ('a, 'b) or_type) (f : 'a -> 'c) (g : 'b -> 'c) : 'c =
  match disj with Left a -> f a | Right b -> g b
```

```
$ python3 tools/ocamlproof.py examples/ocaml/curry_howard.ml
proved modus_ponens_or : (∀ A : Type 0, (∀ B : Type 0, (∀ C : Type 0, (or_type(A)(B) → ((A → C) → ((B → C) → C))))))
...
Lean 4: accepted, 5 of 5 theorems depending on no axioms
```

`examples/ocaml/curry_howard.ml` (the sketch's examples) and
`examples/ocaml/logic.ml` (currying, contraposition, a De Morgan direction,
distributivity, refutation under a constructor, a recursive type) are
checked by both kernels in `tests/test_ocaml.py`.

**OCaml is not a consistent logic**, so what the fragment leaves out is part
of the claim:

- `let rec` — `let rec f x = f x : 'a -> 'b` would prove anything;
- literals, `int`, `bool`, `if`, lists — values, not propositions;
- `when` guards and nested constructor patterns — not yet lowered;
- exceptions, `Obj.magic` — refused by the front end already.

Nothing in the lowering is trusted: a mistake there is a kernel refusal,
and every theorem is re-checked by Lean from the exported file.

## Compiling OCaml to run (`tools/ocaml2rust.py`)

`python3 tools/ocaml2rust.py prog.ml -o prog.rs`, then
`python3 -m shivyc.main prog.rs -o prog`. The Rust is ordinary Crust Rust,
one file.

| OCaml | Rust |
|-------|------|
| `int` | `i64`, every `+ - *` reduced to 63 bits and sign-extended (`ml_wrap`): exact OCaml arithmetic, overflow included |
| `bool`, tuples | `bool`, tuples |
| `unit` | nothing: a unit parameter is dropped, a unit result is no result |
| `a -> b -> c` as a value | `fn(A, B) -> C` |
| `'a tree` at `int` | its own `enum ml_tree_int`, one per instantiation |
| a variant's payload of variant type | `*mut` to it: shared, as OCaml values are |
| `'a list` | `enum ml_list_A { Nil, Cons(A, *mut ml_list_A) }` |
| a polymorphic function | one `fn` per instantiation the program uses |
| `let rec go .. in` | lambda-lifted, captures as leading parameters |
| a self-call in tail position | a jump: the body is a `loop`, the call assigns the parameters and `continue`s |
| `match` | tests and projections along paths, `&&`-short-circuited |
| `print_int`, `print_newline` | `print!`, `println!` |

**Memory.** A constructor allocates with `malloc` and nothing is freed:
OCaml's collector is replaced by an arena that is never returned. Right
for a program that runs and finishes; wrong for a server.

**Stack.** A tail call is a jump, so a loop written as tail recursion runs
in constant stack (a million steps in 256 KB is a test). Deep *non*-tail
recursion, which OCaml also allows, gets room from `main` raising the soft
stack limit to the hard one.

**Checked against.** `tools/ocamlinterp.py`, a direct evaluator of the same
typed AST with OCaml's semantics: 63-bit integers, truncating `/` and `mod`,
constant-space tail calls. `tests/test_ocaml_rust.py` compiles every
program in `examples/ocaml/run/`, runs it, and compares with the
interpreter *and* with the output pinned in the test. Arguments are
evaluated left to right in both; OCaml leaves the order unspecified.

**Refused, by name:** partial application, a closure that captures a
variable used as a value (a closed `fun` is lifted and passed as a `fn`),
`=` on anything but ints and bools, a variant inside a tuple payload
(`of ('a t * int)`: write `of 'a t * int`). Dividing by zero is a machine
fault where OCaml raises `Division_by_zero`.

**Proofs about compiled OCaml.** A function carries its contract as OCaml
item attributes, which OCaml itself ignores:

```ocaml
let iabs2 x = if x < 0 then 0 - x else x
  [@@requires x > min_int] [@@ensures result >= 0]
```

The front end types each clause as a `bool` over the parameters (and
`result`); the emitter writes them as `#[requires]`/`#[ensures]`, with exact
arithmetic, since a specification states the mathematical value. OCaml's
`int` is `ml_int` in the Rust -- an `i64` the lift knows to be 63-bit -- and
`ml_wrap(e)` lifts as `e` with the obligation that `e` stays in 63 bits,
where the wrap is the identity. So `rustprove` proves the contract about the
compiled code and, separately, that no arithmetic in it wraps: for
`iabs` without the `requires` that obligation stays open, because `abs
min_int` is `min_int` in OCaml; with it, the obligation is proved. Lean 4
accepts every settled obligation with no axioms (`TestProvedOCaml`). Crust
also checks the contract at run time.

**Division.** `/` and `mod` truncate toward zero, in the model as in OCaml
and Rust. Each owes a divisor that is not zero (OCaml's
`Division_by_zero`), and `/` owes staying in 63 bits: only `min_int / -1`
leaves them, and OCaml wraps it to `min_int` -- which the emitted code now
does too (`ml_wrap(a / b)`; it printed 2^62 before).

**Recursion.** A recursive function carries `[@@variant e]`, an `int`
measure. A recursive call is read as a variable `g` about which a proof
may assume only the contract, and only for arguments meeting the
`requires` with a smaller, non-negative variant; each call site owes
exactly that as an obligation of its own. Every theorem about the function
-- contract and safety alike -- is then the step of a well-founded
induction on the variant, whose conclusion is the theorem for the function
itself (as Dafny and Why3 reason about recursion). A function that calls a
recursive one is proved with the callee's contract as a hypothesis, the
callee's own theorems proving it.

**Tail recursion.** ocaml2rust compiles a tail-recursive function to
`let mut s = p; .. loop { .. continue .. }`. The lift recognises that
shape -- its only state is the slots, whatever the body declares is fresh
each time round -- and reads each `continue` back as the recursive call on
the slots' current values; the same induction proves it.
`examples/ocaml/run/{division,recursion,tailrec}.ml` pin what is proved
and what is left open (`sum_to`'s `+` does overflow for large `n`, an
unbounded accumulator can wrap, `min_int / -1` does) in
`TestRecursionAndDivision`, with Lean agreeing on every settled one.

Not lifted yet: data enums and the pointers into them, so list and tree
code compiles and runs but is not proved; mutual recursion.

### Fixes to the Rust front end this needed

Each is pinned in `tests/test_ocaml_rust.py` (`TestRustFixes`):

- An enum whose payload is a struct -- a user struct, a `Vec`, a `Box` --
  did not compile: data enums were emitted before every struct. They now
  go through the structs' dependency sort, their tags and declarations
  first, so `Cons(i64, Box<List>)` inside `List` works too.
- A tuple literal ignored its expected type: `(1, 2)` passed as `(i64,
  i64)` was built as a different C struct.
- A variant's payload types were not its constructor's signature.
- A block that was another block's tail lost its value
  (`fn f() -> i64 { { let a = 1; if a == 1 { 5 } else { 6 } } }` returned
  garbage), and a unit `fn main` exited with its last `printf`'s byte
  count. `return 3;` from a unit `main` still sets the exit status.

Also found and not fixed: Crust loses a function whose attributes share its
line (`#[ensures(..)] fn f ..`); the emitter puts each on its own line.

Found and *not* fixed: a function named like an x86 register (`bx`, `ax`,
`cl`, `si`, ..) is miscompiled -- `call bx` assembles as a call through the
register. Emitted names all start with `ml_`, so compiled OCaml cannot hit
it; hand-written C and Rust can.
