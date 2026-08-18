#!/usr/bin/env python3
"""Regression test: taking the address of an `extern` object.

An object declared `extern` and defined elsewhere -- in another translation
unit, or by a *linker script* -- lives at a symbol. Its address must be formed
from that symbol (`lea` / `adrp+add` / `lla`), never from the frame pointer.

This was broken on arm64 and riscv64 and correct on x86-64, and it was broken
*silently*: `&__linker_sym` produced an x29-relative address, so the code
compiled, linked and ran, and simply read the stack instead of the symbol.
Nothing in the differential corpora could see it, because a hosted test program
compiled and run under qemu never references a symbol it does not itself
define -- the bug needs a linker script to become reachable, and a linker
script only appears in the bare-metal path.

That is what makes this worth a dedicated test rather than another corpus
entry: the check is on the *emitted assembly*, not on an exit code, because
there is no exit code that distinguishes the two.

    python3 tools/extern_symbol_test.py
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Each case: source, and the per-target regex the emitted assembly must match.
# The negative check is shared: no frame-relative address may be produced for
# the extern's address.
CASES = [
    ("address of an extern scalar", """
extern unsigned long __linker_sym;
unsigned long get(void) { return (unsigned long)&__linker_sym; }
"""),
    ("address of an extern array", """
extern char __bss_start[];
unsigned long get(void) { return (unsigned long)__bss_start; }
"""),
    ("extern used as a pointer base", """
extern unsigned long __pgtbl_start;
unsigned long first(void) {
    unsigned long *p = &__pgtbl_start;
    return p[0];
}
"""),
]

# What a correct address formation looks like, per target.
WANT = {
    "x86_64": re.compile(r"lea\s+\w+,\s*\[__\w+\]"),
    "arm64": re.compile(r"adrp\s+\w+,\s*__\w+"),
    "riscv64": re.compile(r"lla\s+\w+,\s*__\w+"),
}

# A frame-relative address formation, which is the bug.
BAD = {
    "x86_64": re.compile(r"lea\s+\w+,\s*\[rbp"),
    "arm64": re.compile(r"add\s+\w+,\s*x29,"),
    "riscv64": re.compile(r"addi\s+\w+,\s*sp,"),
}


def compile_asm(src_text, target, workdir):
    src = os.path.join(workdir, "t.c")
    out = os.path.join(workdir, "t.s")
    with open(src, "w") as f:
        f.write(src_text)
    env = dict(os.environ)
    env["SHIVYC_RASM"] = "1"
    p = subprocess.run(
        [sys.executable, "-m", "shivyc.main", src, "-S", "-o", out,
         "--target", target],
        capture_output=True, text=True, cwd=ROOT, env=env)
    if p.returncode != 0 or not os.path.exists(out):
        return None, (p.stdout + p.stderr).strip()
    with open(out) as f:
        return f.read(), ""


def main():
    npass = nfail = 0
    for name, src in CASES:
        for target in ("x86_64", "arm64", "riscv64"):
            with tempfile.TemporaryDirectory() as d:
                asm, err = compile_asm(src, target, d)
            label = "%s [%s]" % (name, target)
            if asm is None:
                print("  FAIL  %-44s compile failed: %s"
                      % (label, err.split("\n")[0] if err else "?"))
                nfail += 1
                continue
            # The symbol must appear at all -- when the bug was live it was
            # absent from the output entirely.
            if "__" not in asm or not WANT[target].search(asm):
                print("  FAIL  %-44s no symbol-relative address formed"
                      % label)
                nfail += 1
                continue
            if BAD[target].search(asm):
                print("  FAIL  %-44s frame-relative address emitted" % label)
                nfail += 1
                continue
            npass += 1
            print("  PASS  %s" % label)

    print("\nextern symbol addressing: %d pass, %d fail" % (npass, nfail))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
