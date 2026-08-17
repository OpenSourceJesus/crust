"""End-to-end test: ShivyCX arm64 -> rasm -> object -> link -> run.

Each program is compiled to AArch64 assembly by ShivyCX, then assembled two
ways -- by rasm (`rasm_obj.assemble_to_elf(..., "arm64")`) and by
`aarch64-linux-gnu-as` -- and both objects are linked and run under qemu. The
exit codes must match.

This is the level the encoder test cannot reach. `rasm_arm64_test.py` proves
individual instructions encode to the right bytes; this proves the *driver*
around them is right too -- section layout, symbol binding, data directives,
and the relocations left for the linker.

Two link modes:
  gcc   -- link with the cross gcc, so only assembly is under test
  rlink -- link with our own linker, exercising the whole self-hosted path

    python3 rasm_arm64_obj_test.py            # both modes
    python3 rasm_arm64_obj_test.py gcc        # just one
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import rasm_obj                     # noqa: E402

CROSS_CC = "aarch64-linux-gnu-gcc"
CROSS_AS = "aarch64-linux-gnu-as"
QEMU = "qemu-aarch64"
RLINK = os.path.join(HERE, "rlink.py")
RCRT = os.path.join(HERE, "rcrt_arm64.s")


PROGS = [
    ("ret_const", "int main(void){ return 42; }"),
    ("arith", "int main(void){ int a=7,b=3; return a*b+a-b; }"),
    ("locals", "int main(void){ int a=1,b=2,c=3,d=4; return a+b+c+d; }"),
    ("if_else", "int main(void){ int x=5; if(x>3) return 10; else return 20; }"),
    ("while_loop", "int main(void){ int i=0,s=0; while(i<10){s+=i;i++;} return s; }"),
    ("for_loop", "int main(void){ int s=0; for(int i=0;i<8;i++) s+=i*2; return s; }"),
    ("nested_loop",
     "int main(void){ int s=0; for(int i=0;i<4;i++) for(int j=0;j<4;j++) s++;"
     " return s; }"),
    ("call", "int add(int a,int b){return a+b;} int main(void){return add(19,23);}"),
    ("recursion",
     "int fact(int n){ return n<=1 ? 1 : n*fact(n-1); } int main(void){"
     " return fact(5)%%256; }".replace("%%", "%")),
    ("fib",
     "int fib(int n){ if(n<2) return n; return fib(n-1)+fib(n-2); }"
     " int main(void){ return fib(11); }"),
    ("many_calls",
     "int a(int x){return x+1;} int b(int x){return a(x)*2;}"
     " int c(int x){return b(x)+a(x);} int main(void){return c(6);}"),
    ("globals", "int g=17; int main(void){ g=g+8; return g; }"),
    ("global_array",
     "int a[5]; int main(void){ int i; for(i=0;i<5;i++) a[i]=i*3;"
     " return a[4]; }"),
    ("local_array",
     "int main(void){ int a[6]; int i; for(i=0;i<6;i++) a[i]=i+1;"
     " int s=0; for(i=0;i<6;i++) s+=a[i]; return s; }"),
    ("pointers",
     "int main(void){ int x=9; int *p=&x; *p=*p+4; return x; }"),
    ("struct_basic",
     "struct P{int x;int y;}; int main(void){ struct P p; p.x=6; p.y=7;"
     " return p.x*p.y; }"),
    ("bitops",
     "int main(void){ int x=0xF0; int y=0x3C; return (x&y)|(x^y); }"),
    ("shifts", "int main(void){ int x=3; return (x<<5)+(x>>1); }"),
    ("unsigned_ops",
     "int main(void){ unsigned x=250; unsigned y=7; return x/y + x%%y; }"
     .replace("%%", "%")),
    ("char_short",
     "int main(void){ char c=100; short s=200; return (int)c+(int)s; }"),
    ("float_arith",
     "int main(void){ double d=3.5; double e=2.0; return (int)(d*e+d); }"),
    ("float_cmp",
     "int main(void){ double a=1.5,b=2.5; if(a<b) return 33; return 44; }"),
    ("float_conv",
     "int main(void){ int i=7; double d=i; d=d*1.5; return (int)d; }"),
    ("mixed",
     "int sq(int x){return x*x;} int g=4;"
     " int main(void){ int s=0; for(int i=0;i<5;i++) s+=sq(i)+g; return s; }"),
    ("do_while", "int main(void){ int i=0; do{ i+=3; }while(i<20); return i; }"),
    ("switch_stmt",
     "int main(void){ int x=3,r=0; switch(x){case 1:r=10;break;"
     " case 3:r=30;break; default:r=99;} return r; }"),
    ("ternary_chain",
     "int main(void){ int x=7; return x<5?1:(x<10?2:3); }"),
    ("logic_ops",
     "int main(void){ int a=1,b=0; if(a&&!b) return 21; return 0; }"),
]


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _have(tool):
    rc, _, _ = _run([tool, "--version"])
    return rc == 0


def compile_c(name, src, workdir):
    cpath = os.path.join(workdir, name + ".c")
    spath = os.path.join(workdir, name + ".s")
    with open(cpath, "w") as f:
        f.write(src + "\n")
    rc, out, err = _run([sys.executable, "-m", "shivyc.main", cpath,
                         "-S", "-o", spath, "--target", "arm64"])
    blob = (out + err)
    if "not implemented" in blob.lower():
        return None, None, "unsupported"
    if rc != 0 or not os.path.exists(spath):
        return None, None, "shivyc failed: %s" % err.strip()[-160:]
    return cpath, spath, ""


def assemble_rasm(spath, opath):
    with open(spath) as f:
        text = f.read()
    data = rasm_obj.assemble_to_elf(text, "arm64")
    with open(opath, "wb") as f:
        f.write(bytes(data))


def test_one(name, src, workdir, mode):
    cpath, spath, err = compile_c(name, src, workdir)
    if cpath is None:
        if err == "unsupported":
            return "SKIP", "arm64 back end does not support this yet"
        return "ERROR", err

    # oracle: the same .s through GNU as
    gas_o = os.path.join(workdir, "%s_gas.o" % name)
    rc, _, err = _run([CROSS_AS, spath, "-o", gas_o])
    if rc != 0:
        return "ERROR", "GNU as failed: %s" % err.strip()[:160]

    my_o = os.path.join(workdir, "%s_my.o" % name)
    try:
        assemble_rasm(spath, my_o)
    except Exception as e:
        return "FAIL", "rasm failed: %s" % str(e)[:160]

    if mode == "gcc":
        gas_bin = os.path.join(workdir, "%s_gas.bin" % name)
        my_bin = os.path.join(workdir, "%s_my.bin" % name)
        rc, _, err = _run([CROSS_CC, "-static", gas_o, "-o", gas_bin])
        if rc != 0:
            return "ERROR", "linking gas object failed: %s" % err.strip()[:160]
        rc, _, err = _run([CROSS_CC, "-static", my_o, "-o", my_bin])
        if rc != 0:
            return "FAIL", "linking rasm object failed: %s" % err.strip()[:160]
    else:
        # Link against the freestanding runtime, which supplies _start and
        # calls exit() with main's return value. Entering at `main` directly
        # would instead `ret` into whatever x30 happened to hold -- every
        # program would crash identically, and comparing two crashes proves
        # nothing.
        gas_bin = os.path.join(workdir, "%s_gasr.bin" % name)
        my_bin = os.path.join(workdir, "%s_myr.bin" % name)
        crt_o = os.path.join(workdir, "rcrt.o")
        if not os.path.exists(crt_o):
            try:
                assemble_rasm(RCRT, crt_o)
            except Exception as e:
                return "ERROR", "assembling rcrt_arm64.s: %s" % str(e)[:160]
        rc, out, err = _run([sys.executable, RLINK, "-o", gas_bin,
                             gas_o, crt_o])
        if rc != 0:
            return "ERROR", "rlink on gas object: %s" \
                % (err.strip() or out.strip())[:160]
        rc, out, err = _run([sys.executable, RLINK, "-o", my_bin,
                             my_o, crt_o])
        if rc != 0:
            return "FAIL", "rlink on rasm object: %s" \
                % (err.strip() or out.strip())[:160]

    gas_rc, _, _ = _run([QEMU, gas_bin])
    my_rc, _, _ = _run([QEMU, my_bin])
    if gas_rc != my_rc:
        return "FAIL", "exit mismatch: rasm=%d gas=%d" % (my_rc, gas_rc)
    # A negative status means the process died on a signal. Two binaries that
    # both segfault agree on their exit status, so the comparison above would
    # call that a pass while proving nothing -- reject it explicitly.
    if my_rc < 0:
        return "FAIL", "both binaries died on signal %d" % (-my_rc)
    return "PASS", "exit=%d" % my_rc


def main(argv):
    for tool in (CROSS_CC, CROSS_AS, QEMU):
        if not _have(tool):
            print("missing %s -- install the aarch64 cross toolchain "
                  "and qemu-user" % tool)
            return 2

    modes = argv[1:] if len(argv) > 1 else ["gcc", "rlink"]
    os.chdir(ROOT)
    workdir = tempfile.mkdtemp(prefix="rasmarm64obj-")
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "ERROR": 0}
    for mode in modes:
        print("\n== link with %s ==" % mode)
        for name, src in PROGS:
            status, detail = test_one(name, src, workdir, mode)
            counts[status] += 1
            print("  %-5s %-16s %s" % (status, name, detail))

    print("\nrasm arm64 end-to-end: %d pass, %d fail, %d skip, %d error"
          % (counts["PASS"], counts["FAIL"], counts["SKIP"], counts["ERROR"]))
    return 0 if counts["FAIL"] == 0 and counts["ERROR"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
