# REGEX — one engine, four languages

`runtime/crust_re.c` is a small backtracking regular-expression engine. It is
reached from C, from the C++ subset, from RPython through `py2c.py`, and from
guest scripts running on the minipy interpreter. There is one implementation
behind all four, so a fix in the engine is a fix in every language at once.

```
                              ┌─ C             runtime/crust_re.h
                              │
crust_re.c  ──────────────────┼─ C++ subset    runtime/crust_re.hpp   cre::regex
  parser → bytecode → VM      │
                              ├─ RPython       py2c lowering
                              │
                              └─ minipy guest  rpy_lib/crustre.py
```

---

## 1. Why not a library

The obvious move is to link a regex library. Two things rule it out.

The first is the **baremetal targets**. Crust compiles to boards with no libc
allocator, so an engine that calls `malloc` on every match cannot be linked at
all. `crust_re` allocates only from an arena the caller supplies — a stack
buffer, a static array, whatever the target has — and never calls `malloc`
itself.

The second is **one translation unit**. Crust's whole argument is that C++ and
Rust meet as plain C with no boundary between them. A regex library reached
through a foreign call would put back exactly the boundary the project exists
to remove.

musl's POSIX `regcomp`/`regexec` is packaged in `shivyc/musl/regex.py`, but it
is TRE-based and pulls in locale and `wchar` machinery. It remains available
under `--musl`; it is not what the four frontends use.

## 2. Design

The instruction set follows **tinyre** (fy, 2012–2015, zlib licence): a
bytecode of `CHAR`/`SPLIT`/`JMP`/`SAVE` with a backtracking executor. Two
things are deliberately different:

- **Byte-oriented, not UTF-32.** tinyre transcodes the subject to a codepoint
  array up front. Crust's strings are `char*`, so the engine matches bytes.
- **Arena, not a snapshot chain.** tinyre keeps a linked list of `VMSnap`
  backtrack states on the heap. `crust_re` recurses with an explicit step
  budget instead.

A repeat over a single consuming instruction (`a*`, `\d+`, `[a-z]{2,4}`)
compiles to a flat `OP_REP` rather than a split loop, so recursion depth stays
O(1) rather than O(length of match). That matters: a `.*` over a 10 KB subject
would otherwise recurse 10,000 deep.

Catastrophic backtracking is bounded by a step budget, returning
`CRUST_RE_ELIMIT` rather than hanging. The budget spans the whole `exec` call,
not each start position — resetting it per position would let a search burn
`limit × length` steps.

## 3. The supported subset

Literals, `.`, character classes with ranges and negation, the escapes
`\d \w \s \D \W \S \b \B \n \t \r \f \v \xHH`, the anchors `^ $`, capturing and
non-capturing groups, named groups `(?P<name>...)`, alternation, the
quantifiers `* + ?` and their lazy forms on both single items and groups,
`{m,n}` on single items, lookahead `(?=...)` `(?!...)` at any width, and
lookbehind `(?<=...)` `(?<!...)` at fixed width.

Fixed-width lookbehind is the same restriction CPython imposes. Width is
computed by following control flow rather than scanning linearly, so
`(?<=ab|cd)e` is correctly fixed at 2, while a star loop's back edge is
detected and reported as variable.

That covers **284 of the 285** static `re` patterns in the tree. The holdout
uses an inline flag.

### What is refused, and why

Compilation fails with a message rather than guessing:

| Construct | Reason |
|---|---|
| backreferences | not implemented |
| inline flags `(?i)` | not implemented |
| `{m,n}` applied to a **group** | the expansion distributes iterations differently from CPython when the body can match empty, changing capture spans and sometimes the match length |

The last one is the interesting case. It could be made to *look* like it works:
most patterns would match correctly and the divergence only shows on a group
that can match empty. It is refused because a subtly wrong match is worse than
a missing feature — it is used zero times in the tree, and single-item `{m,n}`
(which takes the verified `OP_REP` path) is unaffected.

The same principle governs `re.sub` with a callable replacement and
`subprocess.run` with `check=`/`env=`: they warn rather than lower, because the
correct behaviour is not expressible and the incorrect one is silent.

## 4. Verification

`runtime/crust_re_difftest.py` compares every capture offset against CPython's
`re` over a fixed corpus, every static pattern in the tree, and randomly
generated patterns. Roughly 213,000 comparisons across six seeds currently run
clean: **0 divergences, 0 in-subset refusals.**

```sh
python3 runtime/crust_re_difftest.py            # corpus + 20k random cases
python3 runtime/crust_re_difftest.py -n 200000  # longer run
python3 runtime/crust_re_difftest.py --seed 7   # reproduce
```

The fuzzer is not decoration. Every bug in this engine was found by it, and
none by reading the code:

- the empty-loop guard failed the iteration instead of exiting it — CPython
  runs exactly one empty iteration of `(a*)*` and *commits its captures*, so
  `(a*)*b` against `"b"` yields group 1 = `(0,0)`, not unset (346 divergences)
- alternation re-wrapped the program on every `|`, shifting instructions and
  leaving previously recorded patch indices pointing at the wrong one (300)
- the step budget reset per start position (surfaced as runs timing out)
- bounded group repeats lacked the empty-iteration guard
- block copies relocated `OP_SPLIT` and `OP_JMP` targets but not `OP_LOOK`'s,
  so a lookaround inside an alternation kept a stale jump-over target and
  control fell into its own sub-program, reaching `OP_LOOKEND` as though it
  were `OP_MATCH` (610)

Every one of those produces a plausible-looking match. That is the argument for
differential testing over inspection in this area.

## 5. The four frontends

### C

```c
#include "crust_re.h"

char arena[8192];
const char *err = 0;
crust_re *re = crust_re_compile("(\\w+)=(\\d+)", arena, sizeof arena, &err);
if (!re) { /* err names the problem */ }

int caps[8];
if (crust_re_exec(re, text, strlen(text), 0, caps, 8) == CRUST_RE_MATCH) {
    /* caps[0],caps[1] = whole match; caps[2i],caps[2i+1] = group i; -1 unset */
}
```

`crust_re_arena_hint(pat)` sizes a buffer without a trial compile.
`crust_re_group_index(re, "name")` resolves a `(?P<name>)`.

### C++ subset

`std::regex` is out of reach for the subset — it wants exceptions, locales and
iterators. `runtime/crust_re.hpp` supplies `cre::regex` over the same C engine,
written *in* the subset rather than special-cased in the lowering, for the same
reason the supplied `string` and `vector` are: if it compiles, the subset's
claims hold.

```cpp
cre::regex re("(\\w+)=(\\d+)");
cre::smatch m;
if (cre::regex_search(re, "port=8080", m)) {
    char buf[64];
    m.str(1, buf, sizeof buf);      // "port"
}
```

A bad pattern reports through `ok()`/`error()` rather than throwing.

Note that `regex_matches(re, text)` and `regex_contains(re, text)` are **not**
overloads of the two-argument forms. Method overloading is part of the subset;
free-function overloading is not, because free functions lower to plain C
names. Two `regex_match`es collide in the generated C — g++ accepts the header,
`tools/cpprust.py` does not. `runtime/run_cpp_test.sh` builds the same source
both ways and diffs the output so the host compiler cannot mask that again.

### RPython (`py2c.py`)

Two tiers. A pattern inside the narrow subset `regex_parse` accepts lowers to a
**specialized C matcher** — no engine, no bytecode, just the comparisons that
pattern needs. Everything else goes to the VM. A program whose patterns all fit
tier 1 links no engine at all.

Supported: `re.search`, `re.match`, `re.compile` (constant *or* runtime
pattern), `re.findall`, `re.finditer`, `re.sub` (string replacement),
`re.escape`, and on a match `.group(n)`, `.start(n)`, `.end(n)`.
`pat.search(text, pos)` matches from an offset.

Whether `crust_re` accepts a pattern is deliberately **not** re-decided in
Python. Mirroring the parser is exactly the kind of duplicated rule set that
drifts and then lies. Instead the generated program compiles every pattern at
startup and aborts naming the pattern and the engine's own message:

```
regex: cannot compile (?<=x)y: extension group '(?...)' is not supported
```

Patterns are compile-time constants, so that fires on the first run, never
intermittently.

### minipy guests

`import re` in a guest script resolves to `tools/rpy_lib/crustre.py`, which
calls the `__re_search`/`__re_match` builtins. Those are implemented in
`tools/minipy/interp.py` — itself an RPython program compiled by `py2c` — with
a runtime-valued `re.search`, which lowers to the engine. So:

```
guest script → crustre.py → __re_search builtin → interp.py → crust_re
```

`crustre.py` deliberately does not `import re`: minipy maps `re` to that very
module, so importing it would recurse.

## 6. The match value

A match is a list:

```
[g0, g1, ..., gn, s0,e0, s1,e1, ..., sn,en]
```

Captured strings first, spans appended. Length is always `3*(ng+1)`, so a
consumer recovers the group count from the length alone.

Keeping the strings first is what makes `.group(i)` a plain index, and appending
rather than restructuring is what kept every pre-existing consumer working when
spans were added. It is also truthy exactly when it matched, so `if m:` needs no
new runtime type.

**Five producers build this layout** — the tier-1 specialized matchers, the
tier-2 VM bridge, the dynamic bridge, the minipy builtin, and its reference-VM
twin. They have to stay in agreement; that is the main structural risk in the
engine's integration, and the first thing to check if a match ever looks wrong.

## 7. minire

`tools/rpy_lib/minire.py` is a separate pure-Python engine, for contexts with no
compiled core to call into. It is **not** kept feature-matched with `crust_re`,
and that divergence is deliberate rather than an oversight — keeping two
implementations in step is the duplication this design avoids.

What it does guarantee is that it never lies. Anything outside its subset raises
`MinreError` at compile time. Before that was true, an unsupported metacharacter
fell through to the literal branch and was wrong in *both* directions with no
diagnostic:

```python
minire.match('a|b', 'b')      # None    (CPython: 'b')
minire.match('a|b', 'a|b')    # 'a|b'   (CPython: None)
```

`test_minire.py` embeds a verbatim copy of `minire.py`, because minipy has no
module system. `tools/rpy_lib/sync_test_minire.py --check` guards the two
against drift and runs in `make testminipy`.

## 8. Layout

```
runtime/crust_re.h            public C API
runtime/crust_re.c            parser, bytecode, VM
runtime/crust_re.hpp          cre::regex for the C++ subset
runtime/crust_re_difftest.py  differential fuzzer vs CPython re
runtime/crust_re_difftest.c   its subject-under-test driver
runtime/crust_re_cpp_test.cpp C++ frontend test
runtime/run_cpp_test.sh       builds it with g++ AND cpprust, diffs output
tools/pack_crust_re.py        packages the C source for py2c to inline
tools/rpy_lib/crust_re_src.py generated by the packer — do not edit
tools/rpy_lib/crustre.py      the `re` module minipy hands to guests
tools/rpy_lib/minire.py       independent pure-Python engine
```

`crust_re_src.py` is generated. Edit `runtime/crust_re.{h,c}` and re-run
`python3 tools/pack_crust_re.py`; `make testminipy` checks it is in sync.

## 9. Tests

All of these run in `make testminipy`:

| Check | What it proves |
|---|---|
| `crust_re differential vs CPython re` | the engine agrees with the reference |
| `crust_re C++ frontend (host == cpprust)` | the header lowers, not just compiles |
| `crust_re packaged source in sync` | `crust_re_src.py` is not stale |
| `minire embedded copy in sync` | `test_minire.py` has not drifted |
| RPython frontend / spans / finditer / subprocess | `cpython == py2c native` |
| `tools/minipy/test_re_*.py` | 3-way cpython / ref / native for guests |
