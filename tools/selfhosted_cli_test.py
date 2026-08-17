"""End-to-end test of the *self-hosted CLI* path, per target.

This is the path a board actually uses: `python3 -m shivyc.main prog.c -o prog`
with SHIVYC_RASM/SHIVYC_RLINK set, so the compiler, assembler, linker and
runtime are all ours and nothing external is invoked. The other end-to-end
testers drive rasm and rlink directly; this one goes through the command line,
which is where the target has to be *plumbed* rather than merely supported.

Three plumbing bugs lived here, all invisible to the direct testers:

  - `--target` defaulted to x86_64 unconditionally, so running ShivyCX
    natively on an AArch64 board (Raspberry Pi OS 64-bit, Jetson Nano / L4T)
    emitted x86-64 assembly.
  - `assemble()` called rasm with no architecture, so `--target arm64` under
    SHIVYC_RASM produced an x86-64 object from AArch64 assembly.
  - The link step hardcoded the x86-64 runtime and cached its C half under one
    architecture-neutral name.

    python3 tools/selfhosted_cli_test.py            # every available target
    python3 tools/selfhosted_cli_test.py arm64      # just one

Needs qemu-user for the cross targets.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TARGETS = [
    # (target, qemu runner or "" for native)
    ("x86_64", ""),
    ("arm64", "qemu-aarch64"),
    ("riscv64", "qemu-riscv64"),
]

# Each program returns a value the harness checks, and prints on the way so the
# assembly runtime (puts/putint/putchar) is exercised too, not just the exit
# status. Nothing here uses printf: the C half of the runtime is variadic and
# only x86-64 lowers VaSaveBase, so on the other targets a program links
# against the assembly runtime alone.
PROGS = [
    ("squares", "int putint(long n); int putchar(int c);"
                " int main(){int s=0,i; for(i=1;i<=10;i++) s+=i*i;"
                " putint(s); putchar(10); return s % 256;}", 129),
    ("recursion", "int fib(int n){if(n<2) return n;"
                  " return fib(n-1)+fib(n-2);}"
                  " int main(){return fib(11);}", 89),
    ("globals", "int g = 17; int main(){g = g * 3; return g;}", 51),
    ("pointers", "int main(){int x=9; int *p=&x; *p = *p + 4; return x;}", 13),
    ("arrays", "int main(){int a[6]; int i,s=0;"
               " for(i=0;i<6;i++) a[i]=i*2;"
               " for(i=0;i<6;i++) s+=a[i]; return s;}", 30),
    ("strings", "int puts(const char *s);"
                " int main(){char *m=\"hello\"; puts(m); return m[1];}", 101),
    ("floats", "int main(){double d=3.5,e=2.0; return (int)(d*e+d);}", 10),
    ("funcptr", "int a(int x){return x+1;}"
                " int main(){int (*f)(int)=a; return f(41);}", 42),
    ("stackargs", "int f(int a,int b,int c,int d,int e,int g,int h,int i,"
                  "int j){return a+b+c+d+e+g+h+i+j;}"
                  " int main(){return f(1,2,3,4,5,6,7,8,9);}", 45),
]


def _run(cmd, env=None, cwd=None):
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd)
    return p.returncode, p.stdout, p.stderr


def _have(tool):
    if tool == "":
        return True
    rc, _, _ = _run([tool, "--version"])
    return rc == 0


def test_one(target, runner, name, src, want, workdir):
    cpath = os.path.join(workdir, "%s_%s.c" % (target, name))
    bpath = os.path.join(workdir, "%s_%s.bin" % (target, name))
    with open(cpath, "w") as f:
        f.write(src + "\n")

    env = dict(os.environ)
    env["SHIVYC_RASM"] = "1"
    env["SHIVYC_RLINK"] = "1"
    rc, out, err = _run([sys.executable, "-m", "shivyc.main", cpath,
                         "-o", bpath, "--target", target], env=env, cwd=ROOT)
    if rc != 0 or not os.path.exists(bpath):
        return "FAIL", "build failed: %s" % (err.strip() or out.strip())[:150]

    cmd = [bpath] if runner == "" else [runner, bpath]
    rc, out, err = _run(cmd)
    if rc < 0:
        return "FAIL", "died on signal %d" % (-rc)
    if rc != want:
        return "FAIL", "exit %d, expected %d" % (rc, want)
    return "PASS", "exit=%d" % rc


def main(argv):
    wanted = argv[1:]
    workdir = tempfile.mkdtemp(prefix="selfhosted-cli-")
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}

    for target, runner in TARGETS:
        if wanted and target not in wanted:
            continue
        if not _have(runner):
            print("\n== %s ==  SKIP (%s not available)" % (target, runner))
            counts["SKIP"] += len(PROGS)
            continue
        print("\n== %s (self-hosted: our compiler, assembler, linker, "
              "runtime) ==" % target)
        for name, src, want in PROGS:
            status, detail = test_one(target, runner, name, src, want,
                                      workdir)
            counts[status] += 1
            print("  %-5s %-12s %s" % (status, name, detail))

    print("\nself-hosted CLI: %d pass, %d fail, %d skip"
          % (counts["PASS"], counts["FAIL"], counts["SKIP"]))
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
