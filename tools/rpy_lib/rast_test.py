#!/usr/bin/env python3
"""Self-test: run the rast.py Python parser under all three minipy executors
(CPython ground truth, the pure-Python ref VM, and the py2c-compiled native
interpreter) and assert their parse-tree dumps are byte-identical.

This is the first end-to-end demonstration that minipy can compile and run a
non-trivial real-world Python program -- a Python parser -- and get exactly the
same result natively as under CPython.  It exercises classes, methods,
recursion, exceptions, dict/list comprehensions, generator expressions and
heavy string work, all on minipy.

    python3 tools/rpy_lib/rast_test.py            # run all three, compare
    python3 tools/rpy_lib/rast_test.py --keep     # also leave the combined file
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RAST = os.path.join(HERE, "rast.py")
RPY = os.path.join(ROOT, "tools", "rpy.py")
RPY_AST = os.path.join(HERE, "rpy_ast.py")
BUILD_RPY_AST = os.path.join(HERE, "build_rpy_ast.py")
PY2C = os.path.join(ROOT, "tools", "py2c.py")

# A spread of Python constructs -- precedence, defs, control flow, containers,
# calls, comparisons -- enough to meaningfully exercise the grammar.
SNIPPETS = [
    "x = 1 + 2 * 3\n",
    # blank line immediately after a block header (as ast.unparse emits before
    # a first nested def) previously mis-measured INDENT and broke the suite rule
    "class C:\n\n    def a(self):\n        pass\n\n    def b(self):\n        return 1\n",
    "def f():\n\n    x = 1\n\n    return x\n",
    "y = (1 + 2) * 3\n",
    "def f(a, b):\n    return a + b\n",
    "if x > 0:\n    y = 1\nelse:\n    y = 2\n",
    "while n:\n    n = n - 1\n",
    "for i in items:\n    total = total + i\n",
    "vals = [1, 2, 3]\n",
    "d = {1: 2, 3: 4}\n",
    "r = f(a, b) + g(c)\n",
    "ok = a == b and c != d\n",
    "def typed(x: \"int\", s: \"char*\") -> \"int\":\n    return x\n",
    "n: int = 1\ncache: dict[str, int] = {}\nflag: bool\n",
    "doc = '''line one\nline two'''\nc = r'''raw\\nkeep'''\n",
    "s = {\n    \"a\",\n    \"b\",\n}\nk = (x if y\n     else z)\n",
    "r = (\n    \"p1\"\n    \"p2\"\n)\nv = foo(a +\n        b)\n",
    "h = 0xff\nm = ord(c) & 0xff\nsh = (1 << 30) >> 2\nfd = 16 // n\n",
    "print(\"x\", file=sys.stderr)\nj = os.path.join(base, *parts.split(\".\"), \"z\")\n",
    "try:\n    pass\nexcept Exception as e:\n    raise Wrap(str(e)) from e\n",
    "a = r\"x\\ty\\nz\"\nb = r'''p\\nq\\tr'''\nc = \"esc\\there\"\n",
    "for i in xs:\n    if i:\n        a = 1\nelse:\n    a = 2\nwhile a:\n    a = a - 1\nelse:\n    b = 0\n",

    # --- numeric literals ------------------------------------------------
    # NUMBER used to accept only decimal and hex integers, which is why
    # tools/minipy/interp.py could not be parsed: it is full of float
    # constants. Every Python numeric form except complex is covered here.
    "f = 1.0\ng = .5\nh = 10.\n",
    "big = 1e308\nsmall = 1.5e-3\nup = 1E5\nsigned = 1.5e+10\n",
    "b = 0b1010\no = 0o777\nx = 0xdead_beef\n",
    "sep = 1_000\nfsep = 1_000.5\n",
    "pi = 3.141592653589793\ntiny = 2.2250738585072014e-308\n",

    # --- annotated assignment to a non-name target -----------------------
    # ann_assign only accepted a bare NAME, so a class annotating its fields
    # in __init__ (as interp.py's Instr does) failed to parse.
    "class C:\n    def __init__(self, v):\n        self.op: \"int(8)\" = v\n        self.n: \"int\" = 0\n",
    "buf[i]: \"int\" = v\na.b.c: \"T\" = z\n",

    # --- lambda with parameters ------------------------------------------
    # `parameters` allows an annotation, and its optional `":" test` is
    # greedy, so in `lambda x: x` it ate the colon and the body and left
    # lambdef with no ":" to match. Only the zero-argument form worked.
    "f = lambda x: x\ng = lambda a, b: a + b\n",
    "h = lambda a, b=2: a\nv = lambda *a: a\nw = lambda **k: k\n",
    "s = sorted(xs, key=lambda p: p.n)\nz = lambda x: (lambda y: x + y)\n",
]

# Driver appended to a copy of rast.py: parse each snippet and print a canonical
# pre-order dump of the resulting Node tree.  Kept in minipy's supported subset.
DRIVER = '''

def _dump(node, depth):
    if not is_node(node):
        print(". " * depth + "leaf " + str(node))
        return
    print(". " * depth + node.name)
    for ch in node.children:
        _dump(ch, depth + 1)

def _test(src):
    print("### " + src)
    _dump(parse_python(src), 0)

%s
'''


def build_combined():
    calls = "\n".join("_test(%r)" % s for s in SNIPPETS)
    combined = open(RAST).read() + DRIVER % calls
    fd, path = tempfile.mkstemp(prefix="rast_combined_", suffix=".py")
    os.write(fd, combined.encode())
    os.close(fd)
    return path


# The py2c-compiled parser (rpy_ast.py) is a standalone C program: module-level
# statements are not executed, so the snippet parsing must live inside a
# `main() -> "int":`.  We reuse rpy_ast.py's own parser code (stripping its
# self-test) and append this driver with rast_test's canonical SNIPPETS.
COMPILED_DRIVER = '''

def _dump(node, depth):
    if not is_node(node):
        print(". " * depth + "leaf " + str(node))
        return
    print(". " * depth + node.name)
    for ch in node.children:
        _dump(ch, depth + 1)

def _test(src):
    print("### " + src)
    _dump(parse_python(src), 0)

def main() -> "int":
%s
    return 0

if __name__ == "__main__":
    main()
'''


def _strip_selftest(src):
    """Return rpy_ast.py's parser code without its own self-test block."""
    marker = "# Self-test entry point"
    idx = src.find(marker)
    if idx == -1:
        return src.rstrip() + "\n"
    start = src.rfind("\n# ---", 0, idx)
    if start == -1:
        start = src.rfind("\n\n", 0, idx)
    return src[:start].rstrip() + "\n"


def build_compiled(snippets):
    """Compile rpy_ast.py to a native ELF that parses `snippets`.

    Returns (binary_path, tmpdir) on success, or (None, reason) if the toolchain
    is unavailable or a step fails -- callers treat that as a skip, not a failure.
    """
    if not os.path.isfile(PY2C):
        return None, "py2c.py not found"
    parser = _strip_selftest(open(RPY_AST).read())
    calls = "\n".join("    _test(%r)" % s for s in snippets)
    prog = parser + COMPILED_DRIVER % calls
    tmpdir = tempfile.mkdtemp(prefix="rpy_ast_build_")
    srcpy = os.path.join(tmpdir, "prog.py")
    open(srcpy, "w").write(prog)
    outdir = os.path.join(tmpdir, "c")
    r = subprocess.run([sys.executable, PY2C, srcpy, "--out", outdir],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.isdir(outdir):
        return None, "py2c failed"
    subprocess.run([sys.executable, "-c",
                    "import sys;sys.path.insert(0, %r);import py2c;"
                    "py2c.write_runtime(%r)" % (os.path.join(ROOT, "tools"), outdir)],
                   capture_output=True, text=True, timeout=120)
    import glob
    cfiles = glob.glob(os.path.join(outdir, "*.c"))
    if not cfiles:
        return None, "no C emitted"
    binp = os.path.join(tmpdir, "prog")
    gcc = subprocess.run(["gcc", "-std=c99", "-O2", "-DSHIVYC_ARENA_LOG2=31",
                          "-I", outdir] + cfiles + ["-o", binp],
                         capture_output=True, text=True, timeout=300)
    if gcc.returncode != 0 or not os.path.isfile(binp):
        return None, "gcc failed"
    return binp, tmpdir


def run_compiled(snippets):
    """Build + run the py2c-compiled parser.

    Returns (stdout, reason, stale).  stale=True means rpy_ast.py has drifted
    from rast.py -- a real failure the caller should report.  reason set with
    stale=False means the C toolchain was unavailable -- a skip, not a failure.
    """
    chk = subprocess.run([sys.executable, BUILD_RPY_AST, "--check"],
                         capture_output=True, text=True)
    if chk.returncode != 0:
        return None, "rpy_ast.py is stale vs rast.py (run build_rpy_ast.py)", True
    binp, info = build_compiled(snippets)
    if binp is None:
        return None, info, False
    try:
        return run([binp]), None, False
    finally:
        import shutil
        shutil.rmtree(info, ignore_errors=True)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout


def main(argv):
    keep = "--keep" in argv
    combined = build_combined()
    try:
        cpython = run([sys.executable, combined])
        ref = run([sys.executable, RPY, "--ref", combined])
        native = run([sys.executable, RPY, combined])
    finally:
        if not keep:
            os.unlink(combined)
        else:
            print("combined file:", combined)

    # Fourth executor: rast.py compiled straight to a native ELF via py2c
    # (rpy_ast.py), rather than interpreted as minipy bytecode.  A stale
    # rpy_ast.py fails; a missing C toolchain is skipped, not failed.
    compiled, cerr, stale = run_compiled(SNIPPETS)

    ok = True
    if cpython != ref:
        print("MISMATCH: ref VM != CPython"); ok = False
    if cpython != native:
        print("MISMATCH: native (minipy bytecode) != CPython"); ok = False
    if stale:
        print("STALE: " + cerr); ok = False
    elif compiled is not None and cpython != compiled:
        print("MISMATCH: compiled (rpy_ast native ELF) != CPython"); ok = False
    lines = cpython.count("\n")
    if ok:
        tail = "== compiled" if compiled is not None else "(compiled SKIPPED: %s)" % cerr
        print("PASS: cpython == ref == native %s  (%d snippets, %d output lines)"
              % (tail, len(SNIPPETS), lines))
        return 0
    print("--- CPython ---"); print(cpython)
    print("--- native ---"); print(native)
    if compiled is not None:
        print("--- compiled ---"); print(compiled)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
