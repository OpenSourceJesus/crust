#!/usr/bin/env python3
"""crustfuzz -- find inputs that crash the compiler instead of diagnosing them.

A compiler has three acceptable outcomes for any input: compile it, or reject
it with a diagnostic. Raising a Python traceback is never one of them, and
`NotImplementedError: unexpected register size` -- a real bug found by
accident while implementing `#[derive(Clone)]` -- is what that looks like from
the outside. It had been reachable by any function returning a 3-byte struct
for as long as struct returns had existed.

That one was found by luck. There are 57 `raise NotImplementedError` sites in
the backend and no way to tell from reading them which are reachable, so this
tool answers the question by construction: generate small programs across the
axes that tend to matter -- type widths, struct layouts, operators, casts,
calling conventions -- and classify what comes back.

    python3 tools/crustfuzz.py                 # all families
    python3 tools/crustfuzz.py --family struct
    python3 tools/crustfuzz.py --show          # print each crashing program

An input that produces a diagnostic is *fine*: this is not looking for
unsupported constructs, only for the ones that fail in the wrong way.
"""

import argparse
import itertools
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Scalar spellings worth crossing with everything else. Deliberately includes
# the awkward widths -- `char` and `short` are where register naming and
# sign extension go wrong.
SCALARS = ["char", "signed char", "unsigned char", "short", "unsigned short",
           "int", "unsigned int", "long", "unsigned long", "float", "double",
           "_Bool"]

# Field lists chosen so the struct sizes land on 1..17 bytes, including the
# ones that are not register widths (3, 5, 6, 7) and the partial high
# eightbytes (9, 11, 13, 14, 15).
STRUCT_BODIES = [
    "char a;",
    "short a;",
    "char a; char b; char c;",
    "int a;",
    "int a; char b;",
    "int a; short b;",
    "int a; char b; char c; char d;",
    "double a;",
    "long a; char b;",
    "long a; short b;",
    "long a; int b;",
    "long a; int b; char c;",
    "long a; long b;",
    "long a; long b; char c;",
    "double a; double b;",
    "char a[3];",
    "char a[7];",
    "int a[3];",
]

OPS = ["+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>",
       "<", ">", "<=", ">=", "==", "!="]


def _c(body):
    return body


def family_struct_return():
    """Return a struct by value, at every size that matters."""
    for i, body in enumerate(STRUCT_BODIES):
        yield ("struct_return[%s]" % body,
               "struct S { %s };\n"
               "struct S f(struct S *p) { return *p; }\n"
               "int main(void) { struct S s; struct S r = f(&s);"
               " return 0; }\n" % body)


def family_struct_arg():
    """Pass a struct by value, at every size that matters."""
    for body in STRUCT_BODIES:
        yield ("struct_arg[%s]" % body,
               "struct S { %s };\n"
               "int f(struct S s) { return 0; }\n"
               "int main(void) { struct S s; return f(s); }\n" % body)


def family_struct_assign():
    for body in STRUCT_BODIES:
        yield ("struct_assign[%s]" % body,
               "struct S { %s };\n"
               "int main(void) { struct S a; struct S b; b = a;"
               " return 0; }\n" % body)


def family_arith():
    """Every operator against every scalar width."""
    for ty, op in itertools.product(SCALARS, OPS):
        if ty in ("float", "double") and op in ("%", "&", "|", "^", "<<", ">>"):
            continue                      # not valid C, a diagnostic is right
        yield ("arith[%s %s]" % (ty, op),
               "int main(void) { %s a = 1; %s b = 1;"
               " int r = (int)(a %s b); return r; }\n" % (ty, ty, op))


def family_cast():
    """Cast between every pair of scalar types."""
    for src, dst in itertools.product(SCALARS, SCALARS):
        yield ("cast[%s->%s]" % (src, dst),
               "int main(void) { %s a = 1; %s b = (%s)a;"
               " return (int)b; }\n" % (src, dst, dst))


def family_compound():
    """Compound assignment, where the widths of the two sides differ."""
    for ty, op in itertools.product(SCALARS, ["+=", "-=", "*=", "/="]):
        yield ("compound[%s %s]" % (ty, op),
               "int main(void) { %s a = 2; a %s 1; return (int)a; }\n"
               % (ty, op))


def family_rust_struct():
    """The same struct sizes, reached through the Rust front end."""
    rust_field = {"char": "i8", "short": "i16", "int": "i32", "long": "i64",
                  "double": "f64", "float": "f32"}
    for body in STRUCT_BODIES:
        if "[" in body:
            continue
        fields, ok = [], True
        for decl in body.split(";"):
            decl = decl.strip()
            if not decl:
                continue
            parts = decl.rsplit(" ", 1)
            if len(parts) != 2 or parts[0] not in rust_field:
                ok = False
                break
            fields.append("%s: %s" % (parts[1], rust_field[parts[0]]))
        if not ok:
            continue
        yield ("rust_struct[%s]" % body,
               "struct S { %s }\n"
               "fn make(v: S) -> S { v }\n"
               "fn main() -> i32 { 0 }\n" % ", ".join(fields))


def family_rust_derive():
    """Derived methods across the same layouts -- how the first bug surfaced."""
    for ty in ["i8", "i16", "i32", "i64", "f64", "bool"]:
        for n in (1, 2, 3, 5):
            fields = ", ".join("f%d: %s" % (i, ty) for i in range(n))
            yield ("rust_derive[%s x%d]" % (ty, n),
                   "#[derive(Clone, PartialEq, Default)]\n"
                   "struct S { %s }\n"
                   "fn main() -> i32 { let a: S = S::default();"
                   " let b: S = a.clone(); 0 }\n" % fields)


def family_nested_struct():
    """A struct containing a struct, at each awkward outer size."""
    for inner in ["char a;", "char a; char b; char c;", "int a;",
                  "long a; char b;"]:
        for outer in ["struct I i;", "struct I i; char t;",
                      "char t; struct I i;", "struct I i; struct I j;"]:
            yield ("nested[%s | %s]" % (inner, outer),
                   "struct I { %s };\nstruct O { %s };\n"
                   "struct O f(struct O *p) { return *p; }\n"
                   "int g(struct O o) { return 0; }\n"
                   "int main(void) { struct O o; struct O r = f(&o);"
                   " return g(r); }\n" % (inner, outer))


def family_union():
    """Unions as values, arguments and returns."""
    for body in ["char a; int b;", "char a; char b;", "double a; long b;",
                 "char a[3];", "char a[7];", "int a; char b[5];"]:
        yield ("union[%s]" % body,
               "union U { %s };\n"
               "union U f(union U *p) { return *p; }\n"
               "int g(union U u) { return 0; }\n"
               "int main(void) { union U u; union U r = f(&u);"
               " return g(r); }\n" % body)


def family_bitfield():
    """Bitfields, which have their own access-width rules."""
    for base in ["int", "unsigned int", "char", "unsigned char", "long"]:
        for widths in ["1", "3", "7", "1; %s b : 2" % base, "31"]:
            yield ("bitfield[%s : %s]" % (base, widths),
                   "struct S { %s a : %s; };\n"
                   "int main(void) { struct S s; s.a = 1;"
                   " return (int)s.a; }\n" % (base, widths))


def family_vararg():
    """Varargs with each scalar type, where promotion rules bite."""
    for ty in SCALARS:
        yield ("vararg[%s]" % ty,
               "int printf(const char *, ...);\n"
               "int main(void) { %s a = 1; printf(\"%%d\", (int)a);"
               " return 0; }\n" % ty)


def family_array_ops():
    """Arrays of each scalar, indexed and passed as pointers."""
    for ty, n in itertools.product(SCALARS, [1, 3, 7, 8]):
        yield ("array[%s x%d]" % (ty, n),
               "int f(%s *p) { return (int)p[0]; }\n"
               "int main(void) { %s a[%d]; a[0] = 1; return f(a); }\n"
               % (ty, ty, n))


def family_unary():
    """Unary operators across every scalar."""
    for ty, op in itertools.product(SCALARS, ["-", "~", "!"]):
        if ty in ("float", "double") and op == "~":
            continue
        yield ("unary[%s%s]" % (op, ty),
               "int main(void) { %s a = 1; return (int)(%sa); }\n"
               % (ty, op))


def family_rust_scalar():
    """Rust scalar widths through arithmetic and casts."""
    rty = ["i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64", "usize",
           "isize", "f32", "f64", "bool"]
    for a, b in itertools.product(rty, rty):
        if a == b:
            continue
        yield ("rust_cast[%s->%s]" % (a, b),
               "fn f(x: %s) -> %s { x as %s }\n"
               "fn main() -> i32 { 0 }\n" % (a, b, b))


def family_vararg_overflow():
    """Variadic calls past the six integer registers.

    The bug this family exists for: every variadic call used to push all its
    arguments, and a standard callee reads its overflow from the top of that
    block -- which held the format pointer. Nothing had more than six
    arguments until `write!` started generating them.
    """
    for n in range(1, 10):
        specs = "".join("%d" for _ in range(n))
        vals = ", ".join(str(i + 1) for i in range(n))
        yield ("vararg_overflow[%d]" % n,
               "int snprintf(char *, unsigned long, const char *, ...);\n"
               "int main(void) { char b[128];"
               " snprintf(b, 128, \"%s\", %s); return 0; }\n"
               % (specs, vals))


FAMILIES = {
    "vararg_overflow": family_vararg_overflow,
    "nested_struct": family_nested_struct,
    "union": family_union,
    "bitfield": family_bitfield,
    "vararg": family_vararg,
    "array_ops": family_array_ops,
    "unary": family_unary,
    "rust_scalar": family_rust_scalar,
    "struct_return": family_struct_return,
    "struct_arg": family_struct_arg,
    "struct_assign": family_struct_assign,
    "arith": family_arith,
    "cast": family_cast,
    "compound": family_compound,
    "rust_struct": family_rust_struct,
    "rust_derive": family_rust_derive,
}

# A traceback, or an exception name at the end of one, means the compiler fell
# over rather than reporting. Anything containing `error:` is a diagnostic and
# is a perfectly good outcome.
_CRASH = re.compile(r"Traceback \(most recent call last\)|"
                    r"^\w*(Error|Exception):", re.M)


def classify(name, src, out_dir, keep):
    suffix = ".rs" if "rust" in name else ".c"
    path = os.path.join(out_dir, "probe" + suffix)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    proc = subprocess.run(
        [sys.executable, "-m", "shivyc.main", "-c", path,
         "-o", os.path.join(out_dir, "probe.o")],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    text = (proc.stderr or "") + (proc.stdout or "")
    if _CRASH.search(text):
        last = [ln for ln in text.strip().splitlines() if ln.strip()]
        return "crash", last[-1].strip()[:88] if last else "?"
    if proc.returncode != 0:
        return "diagnostic", ""
    return "ok", ""


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--family", action="append",
                    help="restrict to one family (repeatable)")
    ap.add_argument("--show", action="store_true",
                    help="print the source of each crashing probe")
    ap.add_argument("--list", action="store_true", help="list the families")
    args = ap.parse_args(argv)

    if args.list:
        for k in FAMILIES:
            print(" ", k)
        return 0

    chosen = args.family or list(FAMILIES)
    out_dir = tempfile.mkdtemp(prefix="crustfuzz_")
    totals = {"ok": 0, "diagnostic": 0, "crash": 0}
    crashes = []
    for fam in chosen:
        gen = FAMILIES.get(fam)
        if gen is None:
            print("unknown family: %s" % fam)
            return 1
        for name, src in gen():
            kind, detail = classify(name, src, out_dir, args.show)
            totals[kind] += 1
            if kind == "crash":
                crashes.append((name, detail, src))
                print("  CRASH  %-34s %s" % (name, detail))

    print("\n%d probes: %d ok, %d diagnostic, %d crash"
          % (sum(totals.values()), totals["ok"], totals["diagnostic"],
             totals["crash"]))
    if crashes:
        print("\ndistinct crash messages:")
        seen = {}
        for name, detail, _ in crashes:
            seen.setdefault(detail, []).append(name)
        for detail, names in sorted(seen.items(), key=lambda kv: -len(kv[1])):
            print("  %3d  %s" % (len(names), detail))
            print("       e.g. %s" % names[0])
        if args.show:
            for name, detail, src in crashes[:10]:
                print("\n--- %s ---\n%s" % (name, src))
    return 1 if crashes else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
