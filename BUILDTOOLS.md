# BUILDTOOLS — lowering `tools/` to native binaries

`buildtools.py` takes the Python scripts in `tools/`, lowers them to C with
`py2c.py`, compiles them, checks the result actually behaves like the script,
and installs the ones that pass.

It lives at the top level rather than in `tools/` on purpose: everything in
`tools/` is a candidate for lowering and this is not. Its cost is dominated by
the compilers it invokes, so a C version would not be meaningfully faster and
would only complicate the bootstrap.

```sh
python3 buildtools.py                 # build + verify everything
python3 buildtools.py --only cpprust  # one tool (repeatable)
python3 buildtools.py --report        # rank what is blocking translation
python3 buildtools.py --install       # copy verified binaries to PREFIX
python3 buildtools.py -v              # compiler output, per-tool blockers
```

Installed binaries go to `/tmp/crusted/usr/bin` unless `--prefix` says
otherwise.

---

## 1. Four stages

```
transpile  ──▶  compile  ──▶  verify  ──▶  install
```

**`verify` is the stage that matters.** `py2c` substitutes `None` for calls it
cannot lower and *warns* rather than failing, so a tool can transpile and
compile perfectly cleanly and still behave nothing like the script it came
from. A build that succeeds proves almost nothing on its own.

So nothing is installed until its native output matches the Python original on
a real invocation, and a tool with no smoke test stays `unverified` rather than
being installed on the strength of a successful build. `SMOKE` in the script
maps a tool to a fixture and an argv; a tool whose run produces a file has the
file diffed instead of stdout.

Statuses:

| Status | Meaning |
|---|---|
| `verified` | native output matches the script — installable |
| `unverified` | builds, but no smoke test, so correctness is unknown |
| `MISMATCH` | builds and runs, and disagrees with the script |
| `compile` / `transpile` | did not get that far |
| `skipped` | deliberately not a candidate, with the reason recorded |

`py2c.py` itself is in the skip list — it is the transpiler, it is complex, and
it is worth keeping flexible. Recording the reason means "not attempted" is
never mistaken for "failed".

## 2. `--report` is the work queue

The ranking is what turns this from a pass/fail into a plan. It counts
constructs `py2c` could not lower, across every candidate, by uses and by how
many tools each affects:

```
  construct                            uses  tools
  subprocess.run()                       30     22
  Yield expression                       23      5
  tempfile.TemporaryDirectory()          20     12
  argparse.ArgumentParser()              17     17
  ast.walk()                             16     10
  os.listdir()                           15      5
```

Both columns matter and they say different things. `os.listdir` has more uses
than `argparse` but touches a third as many tools; `argparse` is the wider
unlock even though it is not the deeper one.

Two ways to clear an entry, and the cheaper one is often the right one:

- **extend `py2c`** so the construct lowers to efficient C — right when the
  construct is common and the lowering is genuinely useful
- **adjust the tool** — right when the Python is doing something the tool never
  needed. `tools/cpprust.py`'s `_iter_template_uses` was a generator consumed
  eagerly by both its callers over a finite string, so it became a function
  returning a list. Real generator support is worth having; it was not worth
  having *for that function*.

## 3. Current state

Measured over 61 candidates:

```
unverified=3   compile=58   skipped=1
```

**No tools are installed yet, and there is no speed comparison.** That is the
honest headline and it should stay in this document until it changes.

The regex family — `re.sub`, `re.escape`, `re.finditer`, `re.compile`,
`re.findall`, and match `.start()`/`.end()` — was the largest coherent cluster
at 132 uses and is now entirely cleared, along with `tempfile.mkdtemp` (28) and
45 of the 75 `subprocess.run` uses. See [REGEX.md](REGEX.md).

`tools/cpprust.py` went from 67 unlowered constructs to **zero**. It still does
not build: its remaining compile errors are two pre-existing `py2c` limitations
that this work only got far enough to expose —

- **closure capture** — `make_closure` referencing outer locals emits them
  undeclared
- **tuple unpacking into a list literal** — `a, b, c = f()` generating an
  assignment *to* a list constructor, which is not an lvalue

Neither is small, and neither has anything to do with regex. *Lowers cleanly*
is not *works*.

Three tools do build — `arm64_difftest`, `m68k_difftest`, `riscv64_difftest` —
and sit at `unverified`. All three call `subprocess.run` with keywords that are
not lowered, so they would be broken at runtime. The gate refusing to install
them is the gate doing its job.

## 4. A note on link flags

`mb_ffi.c` (the ctypes/`dlopen` shim) is linked only when the generated C
actually references it. Linking it unconditionally made its `mb_dom_*` symbols
collide, which reported tools that had compiled perfectly well as link
failures — three of them.

## 5. Adding a smoke test

Add an entry to `SMOKE` in `buildtools.py`:

```python
"mytool.py": {
    "fixture": "examples/crust/owned.cpp",
    "args": ["{fixture}", "-o", "{out}"],
    "output_file": True,      # diff the produced file, not stdout
},
```

`{fixture}` and `{out}` are substituted. Both the script and the binary are run
with the same argv and their exit codes and output compared. Until a tool has
one of these it can never be installed, however cleanly it builds — which is
the intended default.

## 6. minipy is a target too

`minipy` does not come out of `tool_scripts()` — the thing that gets lowered is
`tools/minipy/interp.py`, one file inside a package whose other half
(`compiler.py`) stays on CPython. It is built anyway, by `build_minipy()`,
because it is the same four-stage pipeline and, unlike the other tools, it is
the one binary the self-hosting story depends on.

Its verify gate is stricter than a `SMOKE` entry's single fixture. minipy is a
Python implementation, so "does it work" is a suite question, not a one-shot
diff: every `tools/minipy/test_*.py` is run under CPython and under the native
interpreter and the outputs must match byte for byte. That is the same 3-way
check `make testminipy` performs, folded into the install gate so a regressed
interpreter cannot reach `PREFIX`.

```
$ python3 buildtools.py --only minipy --install
  ok   minipy    verified    20/20 tests match CPython
installed 1 binaries into /tmp/crusted/usr/bin
```

Two mechanical details this target forced, both of which apply to any future
non-`tools/*.py` target:

* `transpile()` takes the source path and the C-file stem separately, because
  they are not the same here (`tools/minipy/interp.py` -> `interp.c`, installed
  as `minipy`).
* `Result` carries an explicit `install_name`. The old code derived it with
  `r.name[:-3]`, which would have installed minipy as `min`.

The installed binary runs bytecode (`.mpyc` or the JSON form), not `.py`
directly — the compiler front end is still CPython-hosted. See MINIPY.md for
where that stands.
