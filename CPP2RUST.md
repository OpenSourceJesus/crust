# cpp2rust — raising to Rust, to be checked

`tools/cpprust.py` lowers the C++ subset to C. `tools/cpp2rust.py` raises the
same subset to Rust. The two are not alternatives: the C is what gets
compiled and run, and the Rust exists to be handed to `rustc` so the borrow
checker can be asked, independently, whether the ownership cpprust inferred
actually holds.

```sh
python3 tools/cpp2rust.py t.cpp -o t.rs
python3 tools/cpp2rust.py t.cpp --check                     # run rustc
python3 tools/cpp2rust.py t.cpp --mode ownership --check
```

The output is never linked and never run. Bodies are `unimplemented!()` or
ownership skeletons. `--check` compiles with `--emit=metadata`, which type-
and borrow-checks without generating code — the whole of what is wanted, and
much faster than a build.

## Why not the Cpp2Rust translation

Popescu et al., *Cpp2Rust: Automatic Translation of C++ to Safe Rust*, PLDI
2026 (<https://doi.org/10.1145/3808266>), is the obvious starting point and
this file departs from it deliberately. The difference is not a disagreement
about their work; it is that the two tools want opposite things.

Cpp2Rust boxes **every** variable into `Value<T> = Rc<RefCell<T>>` and
represents every pointer as a weak `Ptr<T>` carrying an offset. Their §3.1
says why: *"This ensures that any program can be translated."* Ownership and
mutability move to run time, where `borrow_mut` panics, `Weak::upgrade`
fails, and `Ptr::deref` aborts. Their §4.6 then guarantees memory safety on
the grounds that the generated code contains no `unsafe`.

That guarantee is real, and for their purpose — a translated program that
runs, in production, at a cost they measure at between 2% and 6× — it is
exactly right. It is close to useless as a *checker*. Boxing everything is
what makes `rustc` accept everything: the questions were postponed, not
answered. Their own numbers make the point from the other side, since the
optimizer that claws back 71–87% of the boxing does so for speed, and the
uses it cannot remove are precisely the aliasing-heavy code where a static
answer would have been worth most.

So this module inverts the default. **Emit the most restrictive Rust that
still models the C++ faithfully**, so that borrowck has something to reject.
Native ownership, `impl Drop`, `Clone` only where a copy constructor exists,
real `&`/`&mut` for C++ references.

| | Cpp2Rust (PLDI'26) | cpp2rust.py |
|---|---|---|
| Goal | a program that runs | a question rustc can answer |
| Every variable | `Rc<RefCell<T>>` | its own type |
| Pointers | `Ptr<T>`, a weak ref plus offset | `*mut T` |
| Checks land | at run time | at compile time |
| Bodies | fully translated | stubbed, or ownership skeletons |
| Output is | linked and executed | discarded after `--emit=metadata` |
| Front end | clang AST | cpprust's own passes |
| Failure to translate | avoided by construction | reported, per the guiding rule |

What is taken from the paper is the map rather than the route: their Fig. 2
is a careful enumeration of which C++ types have a Rust spelling and which
need a runtime type, and their §4.6 case analysis of what "functional
equivalence" can mean is the reason the caveats below are stated the way
they are.

## What it buys

The refusals cpprust already makes have Rust equivalents, and the point of
the exercise is that the two arrive at them independently:

| cpprust refuses | rustc says |
|---|---|
| destructor and no copy constructor, copied (Rule of Three) | `E0382` borrow of moved value |
| `delete` twice, or a read after one | `E0382`, via `Box` |
| `T &f()` returning a reference | lifetime does not live long enough |
| `goto` with a destructor pending | there is no `goto` |

A worked case. `Owner` has a destructor and no copy constructor:

```cpp
Owner a;
Owner b(a);
printf("%d %d\n", a.head(), b.head());
```

cpprust exits non-zero naming the Rule of Three. The raised Rust gives
`Owner` an `impl Drop` and no `Clone`, so the copy becomes a move, and the
skeleton is:

```rust
let mut a = Owner::new();
let mut b = a;          // Owner b(a)
let _ = &a;             // read here
```

Add the copy constructor and cpprust exits zero while the Rust emits
`a.clone()`. `Differential` in `tools/test_cpp2rust.py` asserts that
agreement in *both* directions — a divergence fails the suite whichever way
it points, because a case where the two disagree is worth looking at
regardless of which one is wrong.

## Where the two languages disagree

### Drop order

The trap, and it is silent. Rust drops struct fields in **declaration**
order. C++ destroys members in **reverse** declaration order, and the base
after all of them. CPPRUST.md already notes this asymmetry from the other
side: Crust's field glue frees in declaration order because that is Rust's
rule, and each language follows its own.

Which means a field-for-field mapping models the wrong language. Fields are
therefore emitted **reversed**, with the base last:

```cpp
class Tally { public: Vec_int samples; Res mark; };
```
```rust
pub struct Tally {
    pub mark: Res,          // destroyed first, as in C++
    pub samples: Vec_int,
}
```

### By-value parameters

Here the two genuinely disagree, and neither is wrong.

cpprust refuses an owning class crossing a call boundary by value, because
in C++ that is a copy no constructor ran for and no destructor will run for.
Rust makes the same syntax a **move**, which is safe — so the raised Rust
accepts what cpprust refuses.

That is not a bug in either tool. It is that C++ by-value is a copy and Rust
by-value is a move, and the C++ subset has no moves at all. The paper meets
the same wall from its own side and calls it out in §7: a move-only type in
a container is ordinary C++ that cannot be expressed, and CPPRUST.md says
the subset's Rule of Three refusal *is* `unique_ptr`'s move-only semantics.
Do not read a quiet rustc here as cpprust being wrong.

### Aliasing

rustc rejects aliasing that is sound. Two `&mut` borrows with syntactically
overlapping lifetimes are an error even when nothing races and nothing
escapes — the paper's §2.2 spends a page on exactly this, and it is why they
reached for `RefCell` in the first place. A diagnostic from this tool is
therefore **a second opinion, not a verdict**.

## Modes

`--mode types` (the default) emits structs, `Drop`, `Clone`, file-scope
function signatures, and method signatures with `unimplemented!()` bodies.
It answers whether the object model is coherent: field types resolve, an
owning class cannot be copied, a signature naming a class is well formed. It
says nothing about statements and cannot, since there are none.

`--mode ownership` adds one `__own_<name>` per file-scope function, carrying
only the statements that move, borrow, construct or destroy something.

### Erasure, and the invariant that keeps it honest

The skeleton drops most of each body. That is deliberate: arithmetic cannot
double-free anything, so leaving it out costs the check nothing while
removing most of what is hard to translate.

Erasure can only make the check **quieter**. rustc is asked about the moves
that survive, and one that was erased is a question it is never asked. The
failure that is *not* allowed is a statement surviving while its subject was
erased — it then names a variable nothing declared, and rustc reports this
pass's own bug dressed as a finding about the source. So the skeleton tracks
what it has declared and emits no statement naming anything else.

Both halves of that were found by getting them wrong. The first version
dropped `Owner a;` — a declaration with no initialiser matched no pattern —
so every copy below it named an undeclared variable. The version after that
erased `printf("%d", a.head())` entirely, which is correct in that the call
moves nothing, and wrong in that C++ *reads* `a` there: with the read gone
the move above it had nothing to collide with, and the Rule of Three case
went silent while cpprust was still reporting it. Reads now survive the
statements around them.

A third was subtler. Statements were split on **newlines**, so a body
written `{ Node *a = new Node(); delete a; }` on one line lost everything
after the first statement and the function was checked for nothing. Splitting
is now on statement boundaries, tracking parenthesis depth and string
literals.

## What is raised

Reusing cpprust's front half. Everything up to `_strip_comments` in
`cpprust.translate` — header splicing, `#if` evaluation, template
monomorphisation, lambda lowering, `auto`, namespace flattening, aliases,
casts — is about getting one translation unit into the subset and none of it
is about C, so all of it is called rather than written twice.

Unlike `cpprust.py` this file may `import cpprust` outright. The subprocess
rule in CPPRUST.md exists because `shivyc/preproc.py` is transpiled by py2c
and an import there becomes an undefined cross-module reference at link time.
Nothing transpiles this file — it is a developer tool, not a compile step —
so the constraint does not reach it.

| C++ | Rust |
|---|---|
| class | `#[repr(C)] struct`, fields reversed |
| destructor, or an owning member | `impl Drop` |
| copy constructor | `impl Clone` |
| destructor and no copy constructor | *no* `Clone` — copies become moves |
| no destructor anywhere | `Clone` (C++ copies it bitwise) |
| constructor, by arity | `T::new`, `T::new_1`, `T::new_2` |
| method, by arity | `fn m`, `fn m_1`, `fn m_2` |
| `virtual` / `= 0` | `trait T_virt` |
| single inheritance | `base` field, emitted last |
| `T *` | `*mut T` |
| `const T &` / `T &` | `&T` / `&mut T` |
| `operator[]`, `operator*` | `fn index_op`/`deref_op` returning `&mut T` |
| `operator=`, `+=`, `==` | `fn assign_op`, `aug_add`, `cmp_eq` |
| `operator->`, `operator T()` | `fn arrow_op`, `fn conv_op` |
| `new T(..)` / `delete p` | `Box::new(T::new..())` / `drop(p)` |
| template instantiation | one struct per instantiation, same mangled name |
| `__cpp_ref(T)` | `const T &` for a class, bare `T` for a scalar |
| a type named but not defined | opaque struct; `--owning` gives it a `Drop` |

Constructor and method arity follow cpprust's scheme exactly, including that
a class with a single constructor keeps the plain `new` whatever its arity.
The two are meant to agree on the symbol, so they agree on the name.

### Pointers are raw, references are borrowed

A C++ `T *` carries no lifetime and may be null. Inventing a `&T` for one
would have rustc check a claim the source never made, which is how a checker
starts reporting things that are not there. Holding a raw pointer is safe in
Rust; only dereferencing is not, and nothing here dereferences.

A C++ *reference* is a different matter. It is non-null and non-reseatable,
which is precisely what a Rust reference claims — so that one is borrowed,
and rustc gets to check the aliasing. The paper makes the same distinction in
§3.2 and for the same reason, though it stops at skipping the `Value<T>` box.

### Bases with fields and non-virtual methods

The paper lists "base classes with fields or non-virtual methods" among its
unsupported constructs (§4). Crust's subset is broader here:
`examples/crust/dispatch.cpp` has a base with a field *and* non-virtual
methods, and `describe` — not virtual — calls `area`, which is. Adopting
their trait mapping would refuse a file cpprust already lowers.

So non-virtual methods go on the trait as **default methods**. A default
method calling a required one is virtual dispatch from a non-virtual caller,
which is the shape needed.

## Not raised

Reported rather than mistranslated:

* **Anonymous unions and structs.** Their members share storage, and giving
  each a Rust field would give each a `Drop` — a double free this pass would
  have *invented* rather than found. cpprust carries them through fine, since
  C has them; name the union and it raises.
* **An operator with no Rust spelling.** Dropped operators make the check
  quieter without saying so, which is the failure mode this whole file is
  arranged against.
* **A type that cannot be spelled**, other than at file scope, where a
  function whose signature will not translate is skipped rather than
  reported — refusing a whole file over one prototype would lose every check
  the classes in it were going to get.

## What a result means

**rustc rejected it.** A second opinion, not a verdict. rustc rejects sound
aliasing. Where it names a moved value, compare it with what cpprust says
about the same class: the two are meant to agree, and a divergence is worth
understanding in either direction.

**rustc accepted it.** Weaker than it sounds. The model is lossy, bodies are
skeletons, and a check that was never expressed cannot fail. It means nothing
was found, not that there is nothing to find.

## Tests

```sh
python3 tools/test_cpp2rust.py
```

Four groups. **Shape** tests assert what has to hold for the check to mean
anything — an owning class has no `Clone`, fields come out reversed, a
skeleton never names a variable it did not declare. **Differential** tests
run both tools over one source and assert they agree. **rustc** tests hand
the output to the borrow checker, and are **skipped** when rustc is not
installed rather than failing: the raising is useful without it, and a
machine with no Rust toolchain should still be able to run the rest.

That skip is currently load-bearing and should not stay that way. The three
tests in it have never run, so the generated Rust is checked only for shape,
and every claim above about what rustc reports is reasoned rather than
observed. Running the suite on a machine with a toolchain is the first thing
to do to this file.

## Where it stops

* Bodies are stubs or skeletons, so nothing checks control flow, arithmetic
  overflow, or anything an expression does.
* The lifetime row of the table above is earned only at the **signature**
  level: `operator[]` returns `&mut T` with an elided lifetime, so the
  borrow is rustc's business, but no skeleton yet builds the two overlapping
  borrows that would make it fire. Iterator invalidation is the obvious next
  target and needs real expression translation rather than erasure.
* A skeleton never calls another skeleton, so nothing is interprocedural.
* Members defined in another translation unit are not raised, for the same
  reason CPPRUST.md gives: translating is not linking.
