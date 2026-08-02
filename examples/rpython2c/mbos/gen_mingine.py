"""gen_mingine.py -- stage the three-language engine for a gcc build.

    python3 gen_mingine.py <baremetalgames-dir> <stage-dir>

ShivyCX splices `#include "mingine.rs"` and `#include "mingine.py"` itself.
gcc cannot, so this lowers both halves ahead of time -- the Rust through
shivyc/crust.py, the rpython through tools/py2c.py -- and writes the resulting
C out *under the original file names* into a staging directory, alongside
copies of mingine.c and scene.c.

Keeping the names is what makes this work without touching the engine sources.
A quoted `#include` resolves relative to the including file first, so the
staged mingine.c finds the staged mingine.rs (which is now C) rather than the
real one. The engine is compiled unmodified; only its dependencies are
substituted.

The alternative -- maintaining a gcc-flavoured fork of mingine.c -- would give
two copies to keep in step, and the whole point of the exercise is that both
builds compile the *same* source.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, REPO)

from shivyc import crust


def lower_rust(src_path, out_path):
    with open(src_path) as f:
        rust = f.read()
    try:
        c_src = crust.translate(rust, path=src_path)
    except crust.CrustError as e:
        raise SystemExit("gen_mingine: crust could not lower %s: %s"
                         % (src_path, e))
    with open(out_path, "w") as f:
        f.write("/* generated from %s by gen_mingine.py -- do not edit */\n"
                % os.path.basename(src_path))
        f.write(c_src)
        f.write("\n")


def lower_rpython(src_path, out_path, workdir):
    """py2c emits a whole project directory; we want the one module's C.

    The generated file includes "shivyc_rt.h" for pymod/pyfdiv. Those two are
    the only runtime symbols mingine.py reaches, and mingine_mbos.c defines
    them, so the include is dropped rather than dragging the runtime header
    into a freestanding kernel build.
    """
    r = subprocess.run([sys.executable, os.path.join(REPO, "tools", "py2c.py"),
                        src_path, "--out", workdir],
                       capture_output=True, text=True)
    name = os.path.splitext(os.path.basename(src_path))[0] + ".c"
    produced = os.path.join(workdir, name)
    if r.returncode != 0 or not os.path.exists(produced):
        raise SystemExit("gen_mingine: py2c failed on %s:\n%s%s"
                         % (src_path, r.stdout or "", r.stderr or ""))

    with open(produced) as f:
        c_src = f.read()
    c_src = c_src.replace('#include "shivyc_rt.h"',
                          "/* shivyc_rt.h dropped: pymod/pyfdiv are provided\n"
                          "   by mingine_mbos.c, and nothing else is used. */\n"
                          "long pymod(long a, long b);\n"
                          "long pyfdiv(long a, long b);")
    with open(out_path, "w") as f:
        f.write("/* generated from %s by gen_mingine.py -- do not edit */\n"
                % os.path.basename(src_path))
        f.write(c_src)


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: gen_mingine.py <games-dir> <stage-dir>")
    games, stage = sys.argv[1], sys.argv[2]

    if not os.path.isdir(stage):
        os.makedirs(stage)

    work = os.path.join(stage, "_py2c")
    if not os.path.isdir(work):
        os.makedirs(work)

    lower_rust(os.path.join(games, "mingine.rs"),
               os.path.join(stage, "mingine.rs"))
    lower_rpython(os.path.join(games, "mingine.py"),
                  os.path.join(stage, "mingine.py"), work)

    # The C halves are copied unmodified; they are what we want compiled.
    for name in ("mingine.c", "scene.c"):
        shutil.copyfile(os.path.join(games, name), os.path.join(stage, name))

    print("gen_mingine: staged mingine.{rs,py} as C in %s" % stage)


if __name__ == "__main__":
    main()
