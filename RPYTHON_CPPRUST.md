# Lowering cpprust.py to C — where it stands

Issue #16 proposes porting `tools/cpprust.py` to RPython so it can be lowered
to C by `tools/py2c.py` and run natively. This is what that actually costs,
measured rather than estimated, and what is left.

The headline is that the port is much further along than "not started":
**py2c already transpiles all 13,120 lines without refusing anything.** The
work is not rewriting cpprust into a subset. It is that the C which comes out
did not compile, and the reasons were nearly all in py2c rather than in
cpprust.

## The number

`tools/rpy_census.py` runs all three passes over a program and prints one
census, so a change moves a number instead of an assertion:

```sh
python3 tools/rpy_census.py tools/cpprust.py
python3 tools/rpy_census.py tools/cpprust.py --errors   # just the count
```

| | at the start | now |
|---|---|---|
| gcc errors in `cpprust.c` | 76 | **0** |
| gcc errors in `cpp_auto.c` | 6 | **0** |
| links into a binary | no | **yes** |
| runs a translation end to end | no | **yes** |
| output matches CPython | — | **not yet** |
| calls substituted with `None` | 11 | 6 |
| container advisories | 77 | 77 |

The three error columns are different kinds of problem and only the first
two matter. An advisory costs a reader nothing if ignored — the program is
correct either way, only slower. A substitution *changes what the program
does*. And a gcc error is invisible to py2c entirely: cpprust transpiled with
zero complaints from py2c and produced C with seventy-six of them, so "does
it lower?" and "does the result compile?" were separate questions and only
the first had an answer.

## What the errors turned out to be

Almost none of them were cpprust stepping outside the subset. They were
py2c's own lowering, and in one case its regex engine, so the fixes benefit
every RPython program rather than this one.

One shape accounted for most of it: **py2c's type oracle and its emitter
disagreeing.** `value_ctype` says a call yields an obj; the emitter emits a
raw `char*` or `long`; the assignment between them does not compile. That
happened for `re.sub` (16 errors), `str.index` (7), `str.count` (5), and in
the mirror direction for `AS_INT` over a value that was already a long. The
lesson each time was that the fix belongs where the *disagreement* is, not in
whichever half is easier to change — teaching `value_ctype` that `.index` is
an int looked right and made things worse, because that function also decides
how a fresh local is *declared*, so it declared `int` for names that
elsewhere held an obj: seven errors traded for seventeen. What tells the two
`.index` lowerings apart is not the receiver's static type (both are plain
`obj` in the generated C) but the helper py2c itself chose, so that is what
the check keys on.

The rest were individual gaps: transitive closure captures that followed
calls but not values, nested tuple targets, a kwargs slot passed to callees
that had none, `tuple()`, `os.path.normpath`, the sandboxed 3-argument
`eval`, integer conditionals boxed for no reason, and `re.sub` with a
function replacement.

### One was a wrong answer rather than a missing feature

`pat.search(s, pos)` handed the engine `text + pos`. That loses the text
*before* `pos`, so a lookbehind there could not see it:

```python
re.compile(r"(?<=;)x").search(";x", 1)
    CPython -> match at 1
    native  -> no match
```

Not a refusal — a different answer, silently. cpprust windows its scans
precisely so the lookbehind still sees the character before the window
(`agg_re.search(look, lo, at)`), so a self-hosted cpprust would have stopped
matching there and translated C++ subtly wrong rather than failing. Fixed by
giving the engine `crust_re_exec_from`, which starts the search at an offset
while keeping the whole subject visible; `len` doubles as CPython's `endpos`.
The differential fuzzer agrees with CPython over 53,121 comparisons with no
divergences.

This is the one to remember when reading the rest: the dangerous defects here
do not announce themselves.

## How each fix was checked

Every one ships a cpython-vs-native agreement test under `tools/rpy_lib/`,
run by `make testminipy`. The harness compiles the same source both ways and
diffs stdout, so a fix that compiles but computes something else fails.

| Test | Covers |
|---|---|
| `test_re_pos_py2c.py` | lookbehind across `pos`, `endpos` anchoring, absolute offsets, windowed finditer |
| `test_varargs_py2c.py` | `*args` with and without `**kwargs` |
| `test_strcount_py2c.py` | `s.count(sub, start, end)` and its edges |
| `test_tuple_py2c.py` | `tuple()` as a dict key and as an `in` member |
| `test_subfn_py2c.py` | `re.sub` with a function, including zero-width matches |
| `test_eval_env_py2c.py` | the sandboxed 3-argument `eval` |
| `test_ifexp_int_py2c.py` | integer conditionals in assignment, return and argument position |
| `test_ospath_py2c.py` | `normpath`, including popping past the root |
| `test_rsplit_py2c.py` | `s.rsplit(sep, maxsplit)`, which is not `split` reversed |

The edges are deliberate. A hand-written `str.count` gets the empty needle
wrong (Python counts the gaps: `"abc".count("")` is 4), and a hand-written
`normpath` gets `..` past the root wrong. Those are the cases in the tests.

`examples/rpython2c/closures/lifted_captures.py` covers the two closure fixes
through `make rpython`, and it is a real regression test rather than a
demonstration: with the py2c changes stashed it fails to compile.

## What is left

The compile errors are gone. What remains is that the binary, which now
runs a whole translation and writes its output file, writes the *wrong*
thing: asked to translate a three-line class it reported

    cpprust: class Box: base class `<garbage bytes>` is not defined above it

so a string somewhere in the lowered code does not outlive the buffer it was
built from. Neither the C compiler nor a smoke run reports that, which is
precisely the argument for building the cpython-vs-native difftest before
trusting any of this.

Six calls still lower to `None`, and they are now the whole remaining list:

* **The clang oracle** — `json.load`, `_json.dump`, `json.loads`,
  `subprocess.check_output`. `auto` deduction falls back to clang, and none
  of that path lowers. It is optional (`--no-clang`), so the first difftest
  can run without it.
* **Two `yield`s in `cpp_auto`** — generators have no lowering at all, and
  the expression is dropped rather than refused, so those two functions
  silently produce nothing.

## What has not been measured yet

Nothing here says the result is *faster*. The binary runs, but it does not
yet agree with CPython, so timing it would be timing the wrong program. The
order is: fix the string lifetime, difftest cpython-cpprust against
native-cpprust over the 43-file litehtml corpus, then measure. Until that
difftest passes, "it lowers and runs" is the only claim being made.

Two things worth knowing before that measurement is designed:

* Translation is no longer the several-minutes-per-file the issue implies.
  After the quadratic scans were removed (issue #6, merged as PR #7),
  `document.cpp` translates in about 15s, of which roughly 4.5s is a clang
  subprocess and JSON AST parse — real external work that a native cpprust
  would not speed up. A native build plausibly reaches 2-3s, which is worth
  having and is not the order of magnitude the issue assumes.
* **ShivyCX has two bugs of its own here**, both in code gcc compiles
  correctly: it segfaults on `m = pat.search(text, pos)`, and it computes 42
  where CPython and gcc both get 65 on a `*args` program. Two of the fixtures
  above therefore live in the gcc-based harness rather than `make rpython`.
  A self-hosted cpprust has to clear those before it can be trusted.
