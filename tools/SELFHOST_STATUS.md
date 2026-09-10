ShivyCX self-hosting -- progress report
=======================================

Transpiler: ShivyC/tools/py2c.py.

The pipeline is a classic two-stage bootstrap:

    stage 0   shivyc/*.py            (the compiler, written in rpython)
                 |  tools/py2c.py     transpile to C
                 v
              generated C  +  shivyc_rt.c   (the runtime/object model)
                 |  gcc                compile + link
                 v
    stage 1   shivyc_native           (the compiler, as a native binary)
                 |  shivyc_native      compile the generated C *again*
                 v
    stage 2   shivycx                 (the compiler, compiled by itself)

`make bootstrap` builds stage 1 and validates it; `make bootstrap2` attempts
stage 2; `make install` installs whichever is furthest along.


Stage 1 -- DONE
---------------
All compiler modules transpile to C and gcc-compile, and the whole thing links
into a single working native binary:

    built native self-host compiler: .../shivyc_native (70 modules linked)

`make bootstrap` then runs a 10/10 smoke test (const, arithmetic, if/else,
while, for, recursion, pointers, arrays, pointer arithmetic, string indexing)
through the native binary and a compile-speed benchmark against gcc.

Compile speed (headerless ~200-line program, median of 3, this machine):

    shivyc_native   ~2.1 s
    gcc             ~0.07 s
    ratio           ~30x slower than gcc

That ratio is dominated by the runtime's allocation model (see below), not by
the algorithms, and is the obvious first optimization target.


What the native stage-1 compiler handles today
----------------------------------------------
Verified end to end (compile + run, correct exit code), built by the native
binary itself: constants; locals; +, -, *, / and folding; all comparisons;
if/else; while; for; nested loops; ternary; modulo; bitwise ops; shifts;
globals (read + write); function calls (literal and variable args); recursion;
pointers (&x, *p, *p = v); arrays (declaration, subscript read/write, loops);
pointer arithmetic (*(p+1), p[i]); char-string-literal indexing ("ABC"[1]).

Runtime note: the object model bump-allocates from one arena and frees it all
at once per compile (no GC). The arena default is 1 GiB (SHIVYC_ARENA_LOG2=30,
overridable at build time); it lives in BSS so only touched pages are
committed. Allocation is currently heavy (hundreds of MB for a few hundred
lines), which both caps input size and drives the compile-speed ratio.


Stage 2 -- IN PROGRESS (the current frontier)
---------------------------------------------
`make bootstrap2` feeds each generated C module back through the stage-1 native
binary. Today that reports:

    self-compiled 0/62 modules
    first blocker: expected '{', got ';'

The blocker is **function-pointer declarations**. The object model emits, in
every type descriptor and struct, members like

    obj (*tostr)(Obj*);
    bool (*eq)(Obj*, obj);

and the native compiler's declarator parser does not yet accept function-
pointer types (it reads `obj (*tostr)(Obj*)` as a function declaration and then
expects a `{` body). Until function pointers parse, no generated module
compiles, so stage 2 is gated behind that single feature.

Highest-leverage next targets (each unblocks a large batch of modules)
---------------------------------------------------------------------
1. Function-pointer declarators (typedefs, struct members, parameters,
   locals). This is the gate for the entire object model and the #1 priority.
2. Designated and nested aggregate initializers -- the type descriptors are
   emitted as `const TypeInfo X = { .name = ..., .tostr = ... };`.
3. The remaining C surface the runtime uses: unions, variadic functions
   (va_list), compound literals, _Bool, wide integer literals.
4. Reduce per-compile allocation so real inputs fit and the gcc gap narrows.

Once stage 2 links a `shivycx`, `make bootstrap2` runs the full `tests/` suite
against it; that is the acceptance gate for declaring the bootstrap complete.


Debugging tips (carried over, still useful)
-------------------------------------------
* A recurring py2c bug class: a 64-bit value (ILValue/CType/pointer/list) held
  by a parameter or field whose *name* matches the int/str type heuristic gets
  silently truncated to C int or coerced to char*. Annotate the param/field
  (`: "object"` or `: "ILValue"`) to override the heuristic.

  This is worth checking FIRST. It accounted for four separate stage-1
  blockers, and the tell is always that the name reads as the wrong type while
  a neighbouring parameter of the same shape is typed correctly:
    - `FuncBody.code` (wasm.py) -- a list of instruction bytes, typed char*;
      23 list_append calls failed to compile.
    - `ValueNumbering.num` (memsafe_elide.py) -- a dict, typed int.
    - `op(code, ...)` (wasm_simd.py) -- an opcode *number*, typed char*, so
      every key was boxed OBJ_STR(<int>) and the OPCODES lookup compared a
      NULL string. This one built and linked fine and then segfaulted in
      strcmp during module init, before reading a line of input. Note that
      `seq(start, ...)` on the next line was typed int correctly -- the only
      difference was the parameter's name.
    - Symptom to watch for: a crash inside a runtime helper (strcmp, dict_find)
      rather than in generated code.

* Host-only code guards. Anything the translator cannot lower -- dynamic
  imports, subprocess, sys.path manipulation -- goes behind
  `if sys.implementation.name != "shivyc":`, which folds to false at
  translation time and drops the block. The guard must *contain* the
  unlowerable call, not merely return early before it: an early return leaves
  the call as a sibling statement that still has to compile.

* Constructs whose absence is silent rather than loud. `bytes()` used to alias
  `str()`, which is correct for a string and wrong for a list of ints -- it
  rendered the list's repr, so wasm's module_bytes() would have written
  "[0, 97, 115, ...]" to disk. `.decode()` had no lowering at all and was
  emitted as a bare call that gcc implicitly declared as returning int.
  `isinstance(x, some_tuple_variable)` resolved only the first class and
  emitted NULL for the rest. When a lowering looks suspiciously simple, check
  what it does to a non-string argument. The new
  `python3 tools/py2c.py <file> --show-object-model` dumps exactly how each
  field/param was typed (POD vs object) so these mismatches are visible before
  they crash at runtime.
* Build a single module's generated C with `-g` to get line numbers under gdb;
  recompile the rest -O0 and remove stale per-test .o files first or the relink
  trips over multiple `main` definitions.
