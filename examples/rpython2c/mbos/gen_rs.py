"""gen_rs.py -- lower a Crust .rs module to C for the mbos build.

    python3 gen_rs.py editbuf.rs build/rs

writes build/rs/editbuf.c (the whole lowered module) and build/rs/editbuf.h
(just its declarations, so the hand-written C can call in without duplicating
the prototypes by hand).

crust.translate emits a single line holding every struct definition, typedef
and function prototype, followed by the function bodies. Splitting there gives
a real header for free -- the declarations the C side needs are exactly the
ones the Rust side actually produced, so the two cannot drift.

This is the compile half of the story. The check half is `rustc`, run
separately by `make check-rs`; nothing here validates the Rust beyond what
crust.py needs to lower it.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, REPO)

from shivyc import crust


def split_prologue(c_src, path):
    """Return (declarations, definitions).

    The declaration line is the one carrying the typedefs. Anything before it
    is the module's leading comment, which belongs in both outputs.
    """
    lines = c_src.split("\n")
    for i, line in enumerate(lines):
        if "typedef struct" in line or ("void " in line and ");" in line):
            return "\n".join(lines[:i + 1]), "\n".join(lines[i + 1:])
    raise SystemExit(
        "gen_rs: no declaration line in the C lowered from %s -- crust.py's "
        "output shape changed, and this script's assumption with it" % path)


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: gen_rs.py <module.rs> <outdir>")

    src_path, outdir = sys.argv[1], sys.argv[2]
    name = os.path.splitext(os.path.basename(src_path))[0]

    with open(src_path) as f:
        rust = f.read()

    try:
        c_src = crust.translate(rust, path=src_path)
    except crust.CrustError as e:
        raise SystemExit("gen_rs: crust could not lower %s: %s" % (src_path, e))

    decls, defs = split_prologue(c_src, src_path)

    if not os.path.isdir(outdir):
        os.makedirs(outdir)

    guard = "MBOS_RS_%s_H" % name.upper()
    h_path = os.path.join(outdir, name + ".h")
    with open(h_path, "w") as f:
        f.write("/* generated from %s by gen_rs.py -- do not edit */\n"
                % os.path.basename(src_path))
        f.write("#ifndef %s\n#define %s\n\n" % (guard, guard))
        f.write(decls)
        f.write("\n\n#endif\n")

    c_path = os.path.join(outdir, name + ".c")
    with open(c_path, "w") as f:
        f.write("/* generated from %s by gen_rs.py -- do not edit */\n"
                % os.path.basename(src_path))
        f.write('#include "%s.h"\n' % name)
        f.write(defs)
        f.write("\n")

    print("gen_rs: %s -> %s, %s" % (src_path, c_path, h_path))


if __name__ == "__main__":
    main()
