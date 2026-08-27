#!/usr/bin/env python3
"""Random-program fuzzer for the wasm back end.

tools/wasm_difftest.py checks a fixed corpus, which is good at catching the
bugs someone thought to write a case for. This generates random integer
programs instead -- nested expressions, mixed widths and signedness, loops and
calls -- and checks each one against gcc. It is aimed at the parts of the wasm
lowering that have no analogue in the register back ends: operand order on the
stack, the truncation of sub-word arithmetic, and the block-dispatch encoding
of control flow.

Deterministic by default (`--seed`), so a failure is reproducible and can be
turned into a difftest case verbatim.

    python3 tools/wasm_fuzz.py --count 300 --seed 7
"""
import os
import random
import subprocess
import sys
import tempfile

NODE = os.environ.get("NODE", "node")
CC = os.environ.get("CC", "gcc")

# Types the integer core supports. Sub-word and unsigned types are included
# deliberately: they are where a stack machine with only i32/i64 has to insert
# real work, and so where a wrong answer is most likely.
TYPES = ["int", "unsigned int", "long", "short", "unsigned char", "char"]
FTYPES = ["float", "double"]

# The shared WASI host. Every module now imports proc_exit (its `_start`
# calls it), so instantiating with an empty import object no longer works --
# the runner has to supply a real host.
RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "wasm_run.js")


def _status(text):
    """Pull the exit status out of the runner's RESULT marker (report mode)."""
    for ln in text.split("\n"):
        if ln.startswith("RESULT "):
            return int(ln.split()[1]) & 0xFF
    raise ValueError("no RESULT marker")


class Gen:
    def __init__(self, rng):
        self.rng = rng
        self.vars = []

    def expr(self, depth):
        r = self.rng
        if depth <= 0 or not self.vars or r.random() < 0.3:
            if self.vars and r.random() < 0.6:
                return r.choice(self.vars)
            return "%d" % r.randint(-1000, 1000)
        op = r.choice(["+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>"])
        a = self.expr(depth - 1)
        b = self.expr(depth - 1)
        if op in ("/", "%"):
            # Division by zero is undefined in C and traps in wasm; the oracle
            # and the module would "disagree" for reasons that are not a bug.
            # Force a non-zero divisor rather than discarding the case.
            b = "((%s)|1)" % b
        if op in ("<<", ">>"):
            # Shift counts out of range are undefined in C but well-defined
            # (masked) in wasm, so pin them to a range where C is meaningful.
            b = "((%s)&7)" % b
        return "((%s) %s (%s))" % (a, op, b)

    def cond(self, depth):
        op = self.rng.choice(["<", ">", "<=", ">=", "==", "!="])
        return "((%s) %s (%s))" % (self.expr(depth), op, self.expr(depth))


def gen_program(rng):
    g = Gen(rng)
    lines = []

    # A helper function, so calls and multi-function modules get exercised.
    ht = rng.choice(TYPES)
    g.vars = ["p", "q"]
    helper = "%s helper(%s p, %s q){ return %s; }" % (
        ht, rng.choice(TYPES), rng.choice(TYPES), g.expr(3))
    lines.append(helper)

    body = []
    g.vars = []
    nvars = rng.randint(2, 5)
    for i in range(nvars):
        t = rng.choice(TYPES)
        name = "v%d" % i
        body.append("  %s %s = %s;" % (t, name, g.expr(2)))
        g.vars.append(name)

    # `i` and `acc` are declared up front because the memory blocks above and
    # the loop below both use them.
    # A loop with a bounded trip count, so a miscompiled exit condition shows
    # up as a wrong answer rather than a hang.
    body.append("  int i;")
    body.append("  int acc = 0;")
    body.append("  for (i = 0; i < %d; i++) {" % rng.randint(2, 12))
    body.append("    if (%s) { acc += (int)(%s); }"
                % (g.cond(2), g.expr(2)))
    body.append("    else { acc -= (int)(%s); }" % g.expr(2))
    body.append("  }")

    if rng.random() < 0.7:
        body.append("  acc += (int)helper(%s, %s);"
                    % (g.expr(1), g.expr(1)))

    # Memory: an array written through both a subscript and a pointer, a
    # struct, and an address taken of a scalar. These exercise the shadow
    # stack, the frame layout and the load/store width selection -- none of
    # which the pure-expression cases touch at all. Indices are reduced mod
    # the array length so nothing runs out of bounds (which would be UB in C
    # and a trap in wasm, i.e. a disagreement that is not a bug).
    if rng.random() < 0.8:
        n = rng.randint(2, 8)
        et = rng.choice(["int", "char", "short", "long", "unsigned char"])
        body.append("  %s arr[%d];" % (et, n))
        body.append("  for (i = 0; i < %d; i++) arr[i] = (%s)(%s);"
                    % (n, et, g.expr(2)))
        body.append("  { %s *ap = arr; ap[(%s) & %d] = (%s)(%s); }"
                    % (et, g.expr(1), n - 1, et, g.expr(1)))
        body.append("  for (i = 0; i < %d; i++) acc += (int)arr[i];" % n)

    if rng.random() < 0.5:
        body.append("  { struct { int a; %s b; int c; } st;"
                    % rng.choice(TYPES))
        body.append("    st.a = (int)(%s); st.b = %s; st.c = (int)(%s);"
                    % (g.expr(1), g.expr(1), g.expr(1)))
        body.append("    acc += st.a + (int)st.b + st.c; }")

    if rng.random() < 0.5:
        body.append("  { int tmp = (int)(%s); int *tp = &tmp;"
                    % g.expr(1))
        body.append("    *tp = *tp + (int)(%s); acc += tmp; }" % g.expr(1))

    # Floating point. Kept in its own block, and reduced to an integer before
    # it reaches `acc`, so a difference shows up in the exit status.
    #
    # Two things are deliberately avoided rather than tested here. Division is
    # excluded because a zero divisor gives an infinity that then converts to
    # an integer -- undefined in C, saturating in wasm, and something else
    # again on x86, so a disagreement would say nothing about the back end.
    # The final conversion is guarded to a range every int can hold for the
    # same reason.
    if rng.random() < 0.7:
        ft = rng.choice(FTYPES)
        body.append("  { %s f0 = (%s)(%s) / 8.0;" % (ft, ft, g.expr(1)))
        body.append("    %s f1 = (%s)(%s) / 16.0;" % (ft, ft, g.expr(1)))
        fop = rng.choice(["+", "-", "*"])
        body.append("    %s f2 = f0 %s f1;" % (ft, fop))
        cmp_op = rng.choice(["<", ">", "<=", ">=", "==", "!="])
        body.append("    if (f0 %s f1) acc += 3; else acc -= 5;" % cmp_op)
        # Clamp with the later *4.0 already accounted for: an out-of-range
        # float-to-int conversion is undefined in C, and the targets disagree
        # about it in a way that says nothing about this back end (x86 yields
        # INT_MIN, wasm's saturating conversion yields INT_MAX). 1e8*4 is
        # comfortably inside int on every target.
        body.append("    if (f2 > 1e8 || f2 < -1e8) f2 = 0.0;")
        body.append("    acc += (int)(f2 * 4.0); }")

    # A variadic call with a randomly-chosen argument count. The callee is
    # emitted once per program (below) and sums exactly `n` int arguments, so
    # the count in the call and the count it reads always agree.
    if rng.random() < 0.5:
        k = rng.randint(0, 6)
        args = ""
        for _ in range(k):
            args = args + ", (int)(%s)" % g.expr(1)
        body.append("  acc += vsum(%d%s);" % (k, args))

    # Function pointers: pick a callee at run time so the choice cannot be
    # folded away, then call through the pointer.
    if rng.random() < 0.5:
        body.append("  { int (*fp)(int) = ((%s) & 1) ? fp_a : fp_b;"
                    % g.expr(1))
        body.append("    acc += fp((int)(%s)); }" % g.expr(1))

    # Struct assignment, which lowers to memory.copy.
    if rng.random() < 0.4:
        body.append("  { struct Pair p, q; p.x = (int)(%s); p.y = (int)(%s);"
                    % (g.expr(1), g.expr(1)))
        body.append("    q = p; acc += q.x + q.y; }")

    # Structs by value, in both size bands: `struct Pair` (8 bytes) is
    # returned by value by the front end and needs the back end's own hidden
    # pointer, while `struct Wide` (48 bytes) already arrives with the front
    # end's sret pointer. The two take different paths and both are worth
    # generating.
    if rng.random() < 0.4:
        body.append("  { struct Pair p = mkpair((int)(%s), (int)(%s));"
                    % (g.expr(1), g.expr(1)))
        body.append("    acc += sumpair(p); }")
    if rng.random() < 0.3:
        body.append("  { struct Wide w = mkwide((int)(%s));" % g.expr(1))
        body.append("    acc += sumwide(w); }")

    body.append("  return (int)(acc % 256);")
    lines.append("struct Pair { int x; int y; };")
    lines.append("struct Wide { int v[12]; };")
    lines.append("struct Pair mkpair(int a, int b){ struct Pair p;"
                 " p.x=a; p.y=b; return p; }")
    lines.append("int sumpair(struct Pair p){ return p.x + p.y; }")
    lines.append("struct Wide mkwide(int b){ struct Wide w; int i;"
                 " for(i=0;i<12;i++) w.v[i]=b+i; return w; }")
    lines.append("int sumwide(struct Wide w){ int i,s=0;"
                 " for(i=0;i<12;i++) s+=w.v[i]; return s; }")
    lines.append("int fp_a(int x){ return x + 1; }")
    lines.append("int fp_b(int x){ return x * 2; }")
    lines.append("int vsum(int n, ...){ __builtin_va_list ap; int s=0,i;"
                 " __builtin_va_start(ap,n);"
                 " for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
                 " __builtin_va_end(ap); return s; }")
    lines.append("int main(void){")
    lines.extend(body)
    lines.append("}")
    return "\n".join(lines) + "\n"


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _run_reporting(cmd):
    """Run the wasm runner in report mode: the program's status arrives as a
    marker on stderr, so the runner's exit code means only host failure."""
    env = dict(os.environ)
    env["WASM_RUN_REPORT"] = "1"
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def main(argv):
    count = 200
    seed = 1
    keep = False
    i = 1
    while i < len(argv):
        if argv[i] == "--count":
            count = int(argv[i + 1]); i += 2
        elif argv[i] == "--seed":
            seed = int(argv[i + 1]); i += 2
        elif argv[i] == "--keep":
            keep = True; i += 1
        else:
            i += 1

    rng = random.Random(seed)
    workdir = tempfile.mkdtemp(prefix="wasmfuzz-")
    runner = RUNNER

    npass = nskip = nfail = nfront = 0
    failures = []
    frontend = []
    for n in range(count):
        src = gen_program(rng)
        cpath = os.path.join(workdir, "f%d.c" % n)
        wpath = os.path.join(workdir, "f%d.wasm" % n)
        with open(cpath, "w") as f:
            f.write(src)

        rc, out, err = _run([sys.executable, "-m", "shivyc.main", cpath,
                             "-o", wpath, "--target", "wasm"])
        if "NotImplementedError" in (out + err):
            nskip += 1
            continue
        if rc != 0 or not os.path.exists(wpath):
            nfail += 1
            failures.append((n, "compile failed: %s" % (out + err)[:300], src))
            continue

        orabin = os.path.join(workdir, "f%d.ora" % n)
        rc, _, err = _run([CC, "-w", "-std=c99", cpath, "-o", orabin])
        if rc != 0:
            nskip += 1          # generator produced something gcc dislikes
            continue

        mine_rc, _, myerr = _run_reporting([NODE, runner, wpath])
        ora, _, _ = _run([orabin])
        if mine_rc != 0 or "RESULT " not in myerr:
            nfail += 1
            failures.append((n, "invalid/trapped: %s" % myerr.strip()[:200],
                             src))
            continue
        mine = _status(myerr)
        if mine == ora:
            npass += 1
            continue

        # Disagreeing with gcc does not by itself implicate the wasm back end:
        # the front end is shared with every other target, so a mistyped
        # expression is wrong everywhere. Ask the x86-64 back end the same
        # question. If it agrees with wasm, both are faithfully lowering an IL
        # that is already wrong, and the bug is in front of us, not below.
        x86 = _x86_answer(cpath, workdir, n)
        if x86 is not None and x86 == mine:
            nfront += 1
            frontend.append((n, "wasm=%d x86=%d gcc=%d" % (mine, x86, ora),
                             src))
        else:
            nfail += 1
            failures.append((n, "mine=%d oracle=%d (x86=%s)"
                             % (mine, ora, x86), src))

    print("wasm fuzz (seed=%d): %d pass, %d wasm-fail, %d front-end, %d skip"
          % (seed, npass, nfail, nfront, nskip))
    for n, why, src in failures[:5]:
        print("\n--- wasm back end, case %d: %s ---\n%s" % (n, why, src))
    if frontend:
        print("\n%d case(s) where the x86-64 back end gives the same wrong "
              "answer as wasm; these are shared front-end bugs, not wasm "
              "ones. First:" % nfront)
        n, why, src = frontend[0]
        print("--- case %d: %s ---\n%s" % (n, why, src))
    if (failures or frontend) and keep:
        print("\nworkdir kept: %s" % workdir)
    # Only a genuine wasm divergence fails the run; a shared front-end bug is
    # reported loudly but is not this back end's regression to own.
    return 1 if failures else 0


def _x86_answer(cpath, workdir, n):
    """Exit status of the same source built by the x86-64 back end, or None if
    it could not build it."""
    binpath = os.path.join(workdir, "x%d.bin" % n)
    rc, _, _ = _run([sys.executable, "-m", "shivyc.main", cpath,
                     "-o", binpath])
    if rc != 0 or not os.path.exists(binpath):
        return None
    rc, _, _ = _run([binpath])
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
