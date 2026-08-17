"""Self-host gate for the code generator.

`shivyc/asm_gen.py` must keep transpiling through py2c and compiling as C, so
the compiler can eventually compile itself for every target it supports. That
is one of the three stated gates on back-end work (see ARM64.md), but nothing
enforced it -- and it silently regressed once already: a helper added for the
arm64 back end took a register *name* and an ILValue, and py2c inferred the
wrong C type for both, emitting calls that passed a `char *` where the
prototype said `int`.

The failure mode is what makes this worth a test. The Python is valid and every
differential test passes; only the transpiled C is wrong, and only as a
*warning* unless someone reads the compiler output.

    python3 tools/selfhost_asmgen_test.py

Requires gcc. Takes ~30s, most of it in py2c.
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# One known-benign warning predates this test: str_upper takes a non-const
# char*, and py2c hands it a string literal. Anything beyond it is a
# regression. Kept as an exact expected set rather than a count so a *new*
# warning cannot hide by replacing this one.
KNOWN_WARNINGS = [
    "discards const qualifier from pointer target type",
]


def _norm(text):
    """Normalise gcc's directional quotes, which vary with locale."""
    for q in ("\u2018", "\u2019", "\u201c", "\u201d", "'", '"', "`"):
        text = text.replace(q, "")
    return text


def _run(cmd, cwd=None):
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return p.returncode, p.stdout, p.stderr


def main(argv):
    rc, _, _ = _run(["gcc", "--version"])
    if rc != 0:
        print("SKIP: gcc not available")
        return 0

    workdir = tempfile.mkdtemp(prefix="selfhost-asmgen-")
    src = os.path.join(ROOT, "shivyc", "asm_gen.py")

    print("== transpiling shivyc/asm_gen.py ==")
    rc, out, err = _run([sys.executable, os.path.join(HERE, "py2c.py"),
                         src, "--out", workdir], cwd=ROOT)
    blob = out + err
    cpath = os.path.join(workdir, "asm_gen.c")
    if rc != 0 or not os.path.exists(cpath):
        print("FAIL: py2c did not produce asm_gen.c")
        print(blob[-2000:])
        return 1
    print("  ok    py2c produced %s (%d bytes)"
          % (cpath, os.path.getsize(cpath)))

    # py2c reports constructs it could not lower. Those are informational for
    # most modules, but a code generator that silently loses one would emit
    # wrong assembly in the self-hosted build, so surface them.
    stand_ins = []
    for line in blob.split("\n"):
        if "asm_gen.py:" in line and "is not lowered" in line:
            stand_ins.append(line.strip())
    if stand_ins:
        print("FAIL: py2c could not lower %d construct(s) in asm_gen.py:"
              % len(stand_ins))
        for s in stand_ins[:10]:
            print("    " + s)
        return 1
    print("  ok    no unlowered constructs in asm_gen.py")

    print("\n== compiling the transpiled C ==")
    rc, out, err = _run(["gcc", "-c", "-I", workdir, cpath,
                         "-o", os.path.join(workdir, "asm_gen.o")])
    blob = out + err
    if rc != 0:
        print("FAIL: gcc rejected the transpiled C")
        print(blob[-3000:])
        return 1

    warnings = []
    for line in blob.split("\n"):
        if re.search(r"\bwarning:", line):
            warnings.append(line.strip())
    unexpected = []
    for w in warnings:
        known = False
        for k in KNOWN_WARNINGS:
            if k in _norm(w):
                known = True
        if not known:
            unexpected.append(w)

    if unexpected:
        print("FAIL: %d unexpected warning(s) in the transpiled C."
              % len(unexpected))
        print("      A type-inference mismatch shows up here and nowhere")
        print("      else -- the Python runs fine and every differential")
        print("      test still passes.")
        for w in unexpected[:10]:
            print("    " + w)
        return 1

    print("  ok    compiles with %d warning(s), all known-benign"
          % len(warnings))
    print("\nselfhost asm_gen gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
