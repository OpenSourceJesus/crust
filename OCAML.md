# OCAML — an OCaml subset for Crust

Two tools over one front end:

| Tool | What it does |
|------|--------------|
| `tools/ocaml.py` | lexer, parser, Hindley–Milner type inference; `python3 tools/ocaml.py FILE.ml` prints each top-level name and its type |
| `tools/ocamlproof.py` | each top-level definition as a proof of its type (Curry–Howard), checked by RosettaMath's `lean4.py` and then by Lean 4 |

Compiling OCaml to run is the next phase (see *Next*).

## The front end (`tools/ocaml.py`)

Grown from the sketch that started it; its AST names are kept. Every node
carries its line, operators parse by precedence, and every expression gets a
type by Algorithm W with let-polymorphism and the value restriction, so a
backend knows what each value *is*.

**In:** variant types (parameterised, recursive, empty: `type void = |`),
aliases; `let`, `let rec`, `let .. and ..`, parameters `x`, `(x : t)`, `()`,
`_`, `(a, b)`, result annotations; integers, `true`/`false`, `()`,
application, constructors (`P (a, b)` for `of int * int` is two arguments,
for `of (int * int)` one tuple, as in OCaml), tuples, lists (`[]`, `::`,
`[a; b]`), `fun`, `function`, `match` with `when` guards, `if`/`else`,
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
strings, floats, chars, arrays, or-patterns, list patterns `[a; b]` (write
`a :: b :: []`), `if` without `else`, loops, `lazy`, `assert`.

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

## Next: compiling OCaml to run

Both targets inside Crust were probed:

| OCaml needs | Rust subset (`crust.py`) | RPython (`py2c`) |
|-------------|--------------------------|------------------|
| variants with payloads, `match` | yes (`enum`, `match`) | classes, `isinstance` |
| recursive variants (lists, trees) | **no**: `Box<L>` inside `L` is refused | yes |
| tuples, destructuring | tuples yes, `let (a, b) = p` **no** | yes |
| polymorphism `'a` | generic `fn` yes, generic `enum` **no** | via the tagged `obj` word |
| higher-order functions | `fn` pointers, closures | yes |
| types for layout | written | inferred from names and annotations |

Lowering to Rust puts the result in reach of `rustproof`/`rustprove` (the
proofs of the rest of this series), with monomorphisation done by the
OCaml front end, which knows every instantiation; it needs recursive enums
and `let (a, b) = p` in `crust.py` first. Lowering to RPython (the sketch's
emitter) runs today's shapes with no compiler changes, but every generated
value would need an annotation for `py2c`, and it reaches the proof tooling
only through the hand-model route.
