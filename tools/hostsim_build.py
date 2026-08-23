"""hostsim_build.py - compile a bare-metal application to run on the host.

    python3 tools/hostsim_build.py examples/baremetal/kernel_arm64.c
    python3 tools/hostsim_build.py app.c -o /tmp/userapp.so

Produces a shared object the controlling process loads with ctypes. The
application's own C is compiled by the host compiler at -O3 and runs at native
speed; the hardware underneath it comes from hostsim/hostsim.c.

This is the ``--fast`` path, and it is a different tool from armulator rather
than a faster version of it. armulator executes AArch64 instructions and can
answer "does this image boot"; it manages roughly 17,000 instructions a
second. This executes no ARM at all and can answer "does this system behave",
across many boards, in wall-clock time. Neither is a substitute for the other,
which is why tools/hostsim_difftest.py checks that they still agree on the
programs both can run.

The application must confine itself to the seam in hostsim/hostsim.h. Code
that reaches past it -- inline assembly, system registers, a hard-coded
peripheral address -- will not compile here, and that is deliberate: a silent
mismatch between what the host runs and what the board runs is exactly the
failure this arrangement could otherwise introduce.
"""

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOSTSIM = os.path.join(ROOT, "hostsim")

#: -O3 because the point of this path is speed; -fPIC because it is a shared
#: object; the warnings because a bare-metal image compiled for a host is
#: exactly where an implicit declaration hides a real mismatch.
CFLAGS = [
    "-O3", "-fPIC", "-shared",
    "-Wall", "-Wextra",
    "-Werror=implicit-function-declaration",
    "-Wno-unused-parameter",
    "-I", HOSTSIM,
]


#: Backend sources compiled into every build.
BACKEND = ["hostsim.c", "accel.c"]


def have_cuda():
    """True when nvcc is on PATH."""
    return shutil.which("nvcc") is not None


def build(sources, out, extra_cflags=None, extra_ldflags=None, cc="gcc",
          verbose=True, cuda=False):
    """Compile `sources` plus the hostsim backend into the shared object `out`.

    With ``cuda=True`` the accelerator is compiled by nvcc from
    hostsim/accel_cuda.cu and linked in; without it, the plain C in
    hostsim/accel.c is used. The CUDA path needs a toolkit and has never been
    run here -- see the warning at the top of accel_cuda.cu.
    """
    for src in sources:
        if not os.path.exists(src):
            raise SystemExit("no such source: %s" % src)

    objects = []
    if cuda:
        if not have_cuda():
            raise SystemExit(
                "--cuda needs nvcc on PATH. Without it the build would "
                "silently fall back to the C accelerator and the result "
                "would look like a GPU build that was simply slow.")
        obj = os.path.splitext(out)[0] + "-accel.o"
        nvcc = ["nvcc", "-O3", "-Xcompiler", "-fPIC", "-DHOSTSIM_CUDA",
                "-I", HOSTSIM, "-c",
                os.path.join(HOSTSIM, "accel_cuda.cu"), "-o", obj]
        if verbose:
            print("[hostsim] %s" % " ".join(nvcc))
        proc = subprocess.run(nvcc, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit("nvcc failed:\n%s%s"
                             % (proc.stdout, proc.stderr))
        objects.append(obj)

    backend = [os.path.join(HOSTSIM, name) for name in BACKEND]
    cmd = [cc] + CFLAGS + list(extra_cflags or [])
    if cuda:
        cmd += ["-DHOSTSIM_CUDA"]
    cmd += list(sources) + backend + objects
    cmd += ["-o", out, "-lpthread"]
    if cuda:
        cmd += ["-lcudart"]
    cmd += list(extra_ldflags or [])

    if verbose:
        print("[hostsim] %s" % " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("host compile failed:\n%s%s"
                         % (proc.stdout, proc.stderr))
    if proc.stderr.strip() and verbose:
        print(proc.stderr.rstrip())
    if verbose:
        print("[hostsim] %s (%d bytes)" % (out, os.path.getsize(out)))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compile a bare-metal application to run on the host.")
    parser.add_argument("sources", nargs="+", help="application C sources")
    parser.add_argument("-o", "--output", default="/tmp/userapp.so")
    parser.add_argument("--cuda", action="store_true",
                        help="compile the accelerator with nvcc for the GPU. "
                             "Needs a CUDA toolkit; see the warning in "
                             "hostsim/accel_cuda.cu.")
    parser.add_argument("--cc", default="gcc",
                        help="host compiler (gcc, g++, clang)")
    parser.add_argument("-D", dest="defines", action="append", default=[],
                        help="preprocessor define, repeatable")
    parser.add_argument("-l", dest="libs", action="append", default=[],
                        help="library to link, e.g. -l m. Reach for this to "
                             "pull in CUDA, BLAS or anything else the host "
                             "has and the board does not.")
    args = parser.parse_args(argv)

    build(args.sources, args.output,
          extra_cflags=["-D" + d for d in args.defines],
          extra_ldflags=["-l" + l for l in args.libs],
          cc=args.cc, cuda=args.cuda)
    return 0


if __name__ == "__main__":
    sys.exit(main())
