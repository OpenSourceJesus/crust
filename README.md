# Crust
## A Unified C/C++/Rust Compiler Environment for Systems Programming

Main Crust Documentation: 
- [CRUST.md](CRUST.md) C+Rust
- [CPPRUST.md](CPPRUST.md) C+C++Rust
- [SHIVYCX.md](SHIVYCX.md) SHIVYC-X (C Compiler)
- [CPP2RUST.md](CPP2RUST.md) C++ to Rust translator

Hardware:
- [BAREMETAL_ARM64.md](BAREMETAL_ARM64.md)
- [RASPI.md](RASPI.md) Raspberry Pi
- [JETSON_NANO.md](JETSON_NANO.md) Nvidia Jetson Nano
- [BAREMETAL_THREADS.md](BAREMETAL_THREADS.md)

Crust Papers:
- https://dx.doi.org/10.2139/ssrn.7315678 "Interoperation Without an Interface: C++ and Rust in One Translation Unit, and One Toolchain"
- https://dx.doi.org/10.2139/ssrn.7226482 "Full-stackless Computing: A Multi-language Compiler That Builds and Boots its Own Operating System"


Modern systems development forces engineers to choose between two paradigms:
- The ubiquitous simplicity and legacy ecosystem of C.
- The type safety, spatial memory guarantees, and modern ergonomics of Rust.

Today, systems that use both languages must compile them through isolated pipelines (clang/gcc and rustc) and stitch them together using dynamic (.so) or static (.a) libraries via a foreign function interface (FFI).

This FFI boundary comes at a steep price:
- Forced C ABI lowerings that strip high-level type metadata.
- Opaque calling conventions that inhibit aggressive interprocedural optimizations (IPO) and register allocation across language boundaries.
- Heavy, fragile Link-Time Optimization (LTO) setups that slow compile times dramatically while still missing frontend-level alias and lifetime optimizations.

Crust solves this by uniting C/C++ and Rust into a single compiler frontend. By natively accepting ISO C syntax alongside a growing subset of Rust syntax, Crust enables seamless, zero-overhead cohabitation of both paradigms inside a single compilation unit.

# C++ and Rust in one file

This is an introduction to how the two languages meet in this project. It
assumes no knowledge of the rest of the documentation; `CRUST.md` and
`CPPRUST.md` are the reference material behind it.

## The one idea

Most attempts to make C++ and Rust talk to each other put something *between*
them: a foreign function interface, a generated shim, a marshalling layer, a
description of one language's types written in the other's. Something has to
translate, and that something costs a call boundary the optimiser cannot see
through.

This project takes the other route. Both languages are lowered to **plain C
source**, and then one C compiler reads the result:

```
foo.rs  ──▶ shivyc/crust.py  ──┐
                               ├──▶ plain C ──▶ ShivyCX ──▶ machine code
foo.cpp ──▶ tools/cpprust.py ──┘
```

By the time anything is compiled there is no C++ and no Rust left, so there
is no boundary to cross. A Rust function calling a C++ method is a C function
calling a C function: same translation unit, same intermediate
representation, same register allocator, and inlining across the two sides is
just inlining.

The cost of this is honesty about scope. Neither front end implements its
whole language, and both **refuse** what they cannot lower rather than
guessing. That refusal is the design, not a gap in it -- a subset you can
trust is worth more than a superset that is quietly wrong somewhere.

## Where the two sides meet

They meet at the **symbol**. Both lowerings were chosen to produce the same
shape of C, so the same data has the same name from either side.

A Rust `impl` method:

```rust
impl Counter {
    fn bump(&mut self, by: i32) { self.n += by; }
}
```

```c
void Counter_bump(Counter *self, int by) { self->n += by; }
```

A C++ class method:

```cpp
class Counter {
public:
    void bump(int by) { n = n + by; }
};
```

```c
void Counter_bump(Counter *this, int by) { this->n = this->n + by; }
```

`&mut self` and `this` are the same pointer. `Type_method` is the same name.
That is not a coincidence and it is not for looks: it is what lets a class
written in one language be used from the other without anything in between.

## Destruction: the same function from both sides

The clearest case is object lifetime, because both languages have opinions
about it and they turn out to agree on the symbol.

A Rust type with a destructor:

```rust
struct Res { id: i32 }

impl Drop for Res {
    fn drop(&mut self) { printf("released %d\n", self.id); }
}
```

lowers to `Res_drop(Res *self)`. A C++ `~Res()` lowers to `Res_drop(Res *)`
as well. So a C++ class can hold a Rust type **by value**, and its destructor
calls the Rust one directly:

```cpp
class Holder {
public:
    Vec_int nums;      /* a Crust Vec<i32> */
    Res     r;         /* a Crust type with impl Drop */
};
```

No destructor is written there. Both members own something, so the class gets
an implicit one that releases each of them -- and `Res_drop` in it is the
function Crust emitted from the `impl Drop` above.

Where the two languages genuinely disagree, each side follows its own rule
rather than one being bent to the other. Members are destroyed in **reverse**
declaration order on the C++ side, because that is C++; Rust's field glue
frees in **declaration** order, because that is Rust. The symbol is shared;
the order is not.

## What each side is for

They are not redundant. Each brings something the other does not.

**Rust** brings move semantics and the ownership discipline around them.
Passing an owning value by value is a move: the source is zeroed, the callee
takes it, and reading the source afterwards is *rejected* rather than
silently yielding an empty value.

**C++** brings a full object lifecycle: constructors chosen by arity, copy
construction and `operator=`, member and base construction ordering,
inheritance and virtual dispatch. Where a Rust `impl Drop` gives a type a
destructor, a C++ class gives it a whole life.

So the natural division is that C++ owns the *structure* -- class
hierarchies, RAII wrappers, anything with a rich lifecycle -- and Rust owns
the *work*, with containers and algorithms that move rather than copy. A
program can use whichever is the better tool for each piece and pay nothing
for mixing them.

## The boundary is checked, not assumed

Sharing a representation means bugs can cross languages, and the passes look
for exactly that. Two examples, both of which were real:

Passing an owned value by value from C++ to a Rust function is **refused**:

```cpp
return consume(t.samples);        /* a Rust fn consume(v: Vec<i32>) */
```

Rust drops a by-value owning parameter when the callee returns, so `consume`
frees the buffer -- and the C++ destructor frees it again. One buffer, two
frees. The diagnostic names the fix, and the fix costs nothing: pass
`&t.samples`, which is what a Rust `&Vec<i32>` parameter lowers to anyway.

Deriving a copy on a type that owns something is **refused** on the Rust
side, for the same reason C++ refuses to copy a class with a destructor and
no copy constructor. It is the Rule of Three, arrived at from two directions.

## A whole file

```c
#include "tally.cpp"

int printf(const char *, ...);

/* Rust: a type with a destructor, and a function that moves a Vec. */
struct Res { id: i32 }

impl Drop for Res {
    fn drop(&mut self) { printf("  released %d\n", self.id); }
}

fn total(v: &Vec<i32>) -> i32 {
    let mut acc: i32 = 0;
    for i in 0..v.len() { acc += v.get(i); }
    acc
}

/* C: the driver. */
int main(void) {
    printf("sum = %d\n", collect());
    return 0;
}
```

with `tally.cpp` alongside it:

```cpp
class Tally {
public:
    Vec_int samples;      /* by value: complete here, no forward declaration */
    Res     mark;

    void add(int v) { Vec_int_push(&samples, v); }
};

int collect(void) {
    Tally t;
    t.mark.id = 7;
    t.add(3);
    t.add(4);
    return total(&t.samples);   /* C++ calling Rust, by pointer */
}
```

It prints:

```
  released 7
sum = 7
```

One translation unit. `Tally` is destroyed at the closing brace of
`collect`, releasing both members -- which is where `released 7` comes from,
and it is the Rust `impl Drop` that printed it, called from a C++ destructor
nobody wrote. `total` is a Rust function reading a C++ member. Nothing is
marshalled anywhere.

## Where to go next

- **`CRUST.md`** -- the Rust subset: what it supports, what it refuses, and
  why. Ownership, `Drop`, moves, and the bundled core containers.
- **`CPPRUST.md`** -- the C++ subset: classes, templates, inheritance and
  virtual dispatch, the supplied `string`/`vector`/`map`/smart pointers, and
  the C++11 spellings.
- **`examples/crust/`** -- working programs, each run by
  `tools/crust_examples.py` against its expected output. `ownmember.c` is the
  owning-member shape, `raii.c` the borrowing one, `cpp11.c` the C++11
  spellings, and `dispatch.c` virtual dispatch with Rust reducing the
  results.

---

# Crust Architectural Philosophy
Function-Level Syntax Isolation
To prevent syntactic ambiguity and parsing complexity, Crust enforces function-level syntax boundaries. A source file contains functions written entirely in standard C syntax alongside functions written in Rust syntax.

---
## Benchmarks
- (June 29, 2026) https://doi.org/10.5281/zenodo.21048364


## References

- [ShivC](https://github.com/ShivamSarodia/ShivC) — the original compiler ShivyCX was rewritten from.
- C11 Specification — http://www.open-std.org/jtc1/sc22/wg14/www/docs/n1570.pdf
- x86-64 ABI — https://github.com/hjl-tools/x86-psABI/wiki/x86-64-psABI-1.0.pdf
- Iterated Register Coalescing (George and Appel) — https://www.cs.purdue.edu/homes/hosking/502/george.pdf
- (B. Hartshorn, viXra 2025, 2026).
    - *Foundational Problems with Compilers and Operating Systems* https://ai.vixra.org/abs/2507.0081
    - *Rethinking Meta-Interpreters for High-Performance Execution* https://ai.vixra.org/abs/2606.0084
    - *Closing the Compiler Loop:
Toward a Self-Hosting, Dependency-Free Python JIT* https://zenodo.org/records/21178004
    - *Beyond WebAssembly: Rethinking the Web Browser with
Post-JavaScript, VM-Free Page Execution* https://ai.vixra.org/pdf/2607.0013v1.pdf

## External Demos
- https://github.com/OpenSourceJesus/ShivyCX-Game-Demos
