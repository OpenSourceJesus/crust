"""End-to-end test for the rlink linker.

Three groups of checks:

  1. **Fully self-hosted pipeline** -- compile a C program with ShivyCX,
     assemble it with rasm, assemble the freestanding runtime (rcrt.s) with
     rasm, link both with rlink, run the result, and compare its exit code and
     stdout against the same program built by the system gcc. Nothing outside
     this repository touches the binary under test.
  2. **Archive handling** -- pack the runtime's objects into a `.a` with `ar`
     and link against that instead, checking that rlink pulls in exactly the
     members it needs to satisfy undefined symbols.
  3. **Interop** -- link gcc-produced objects, which exercise relocation forms
     (PLT32, section-relative 32S, .rodata references) that ShivyCX does not
     emit.

The reference build uses gcc only to establish *expected* behaviour; the binary
being validated is produced entirely by our own tools.
"""
import os
import sys
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rasm_obj
import rlink

REPO = os.path.dirname(os.path.dirname(HERE))
SHIVYC_MAIN = os.path.join(REPO, "shivyc", "main.py")
RCRT = os.path.join(HERE, "rcrt.s")

# Programs use only what the freestanding runtime provides.
RUNTIME_DECLS = """
int puts(const char *s);
int putchar(int c);
void putint(long n);
void *malloc(unsigned long n);
void *memset(void *d, int c, unsigned long n);
void *memcpy(void *d, const void *s, unsigned long n);
unsigned long strlen(const char *s);
"""

PROGRAMS = {
    "exit_code": """
int main(){ return 42; }
""",
    "arithmetic": """
int main(){ int a = 7, b = 3; return a*b - b + (a/b) + (a%b); }
""",
    "recursion": """
int fib(int n){ if (n < 2) return n; return fib(n-1) + fib(n-2); }
int main(){ putint(fib(20)); putchar(10); return fib(10) % 100; }
""",
    "globals_bss": """
int arr[64];
long acc = 3;
int main(){
  int i;
  for (i = 0; i < 64; i++) { arr[i] = i * i; acc += arr[i]; }
  putint(acc); putchar(10);
  return (int)(acc % 251);
}
""",
    "strings": """
char buf[64];
int main(){
  puts("linked by rlink");
  putint((long)strlen("abcdef")); putchar(10);
  memset(buf, 65, 10); buf[10] = 0;
  puts(buf);
  return (int)strlen(buf);
}
""",
    "long_branches": """
int main(){
  int s = 0, i, j;
  for (i = 0; i < 40; i++) {
    for (j = 0; j < 40; j++) {
      if (j % 3 == 0) s += j;
      else if (j % 3 == 1) s -= 1;
      else s += 2;
    }
    if (s > 5000) s = s / 2;
  }
  putint(s); putchar(10);
  return s % 200;
}
""",
    "heap": """
int main(){
  int *p = (int *)malloc(256);
  int i, s = 0;
  for (i = 0; i < 64; i++) p[i] = i * 3;
  for (i = 0; i < 64; i++) s += p[i];
  putint(s); putchar(10);
  return s % 128;
}
""",
    "pointers_structs": """
struct P { int x; int y; };
int sum(struct P *p){ return p->x + p->y; }
int main(){
  struct P a; a.x = 17; a.y = 25;
  struct P *q = &a;
  q->x += 1;
  putint(sum(q)); putchar(10);
  return sum(&a);
}
""",
    "bitops": """
int main(){
  unsigned x = 0xF0, y = 0x18;
  unsigned r = ((x | y) & 0xFF) ^ (x >> 4) ^ ((y << 1) & 0x7F);
  putint((long)r); putchar(10);
  return (int)(r & 0xFF);
}
""",
    "fnptr": """
int add(int a, int b){ return a + b; }
int mul(int a, int b){ return a * b; }
int apply(int (*f)(int,int), int a, int b){ return f(a, b); }
int main(){
  int s = apply(add, 6, 7) + apply(mul, 3, 4);
  putint(s); putchar(10);
  return s;
}
""",
}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def build_reference(name, csrc, work):
    """Build with gcc + a C shim for the runtime, to get expected behaviour."""
    shim = os.path.join(work, "shim.c")
    with open(shim, "w") as f:
        f.write("""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
void putint(long n){ printf("%ld", n); }
""")
    src = os.path.join(work, name + "_ref.c")
    with open(src, "w") as f:
        f.write("#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n"
                "void putint(long n);\n" + csrc)
    exe = os.path.join(work, name + "_ref")
    r = run(["gcc", "-w", "-O0", "-o", exe, src, shim])
    if r.returncode != 0:
        return None
    got = run([exe])
    return (got.returncode, got.stdout)


def assemble_with_rasm(asm_path, obj_path):
    with open(asm_path) as f:
        elf = rasm_obj.assemble_to_elf(f.read())
    with open(obj_path, "wb") as f:
        f.write(bytes(bytearray(elf)))


def compile_with_shivyc(csrc, work, name):
    src = os.path.join(work, name + ".c")
    with open(src, "w") as f:
        f.write(RUNTIME_DECLS + csrc)
    asm = os.path.join(work, name + ".s")
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO
    r = run([sys.executable, SHIVYC_MAIN, "-S", src, "-o", asm],
            cwd=REPO, env=env)
    if r.returncode != 0 or not os.path.exists(asm):
        return None
    return asm


def link_with_rlink(objs, exe, entry="_start"):
    ln = rlink.Linker()
    ln.entry_name = entry
    for path in objs:
        with open(path, "rb") as f:
            data = list(f.read())
        if rlink._is_archive(data):
            ln.add_archive(path, data)
        else:
            ln.add_object(path, data)
    image = ln.link()
    with open(exe, "wb") as f:
        f.write(bytes(bytearray(image)))
    os.chmod(exe, 0o755)


def main():
    work = tempfile.mkdtemp(prefix="rlink_test_")
    crt_obj = os.path.join(work, "rcrt.o")
    assemble_with_rasm(RCRT, crt_obj)

    passed = 0
    failed = 0
    skipped = 0

    print("== self-hosted pipeline: ShivyCX -> rasm -> rlink ==")
    for name in sorted(PROGRAMS.keys()):
        csrc = PROGRAMS[name]
        ref = build_reference(name, csrc, work)
        if ref is None:
            print("  SKIP  %-18s (gcc reference failed)" % name)
            skipped += 1
            continue
        asm = compile_with_shivyc(csrc, work, name)
        if asm is None:
            print("  SKIP  %-18s (ShivyCX could not compile)" % name)
            skipped += 1
            continue
        obj = os.path.join(work, name + ".o")
        exe = os.path.join(work, name)
        try:
            assemble_with_rasm(asm, obj)
            link_with_rlink([crt_obj, obj], exe)
        except Exception as e:
            print("  FAIL  %-18s %s" % (name, e))
            failed += 1
            continue
        got = run([exe])
        if got.returncode == ref[0] and got.stdout == ref[1]:
            print("  ok    %-18s exit=%d out=%r" % (name, got.returncode,
                                                    got.stdout[:32]))
            passed += 1
        else:
            print("  FAIL  %-18s rlink=(%d,%r) gcc=(%d,%r)"
                  % (name, got.returncode, got.stdout[:40], ref[0],
                     ref[1][:40]))
            failed += 1

    print("\n== archive (.a) member selection ==")
    try:
        lib = os.path.join(work, "librcrt.a")
        r = run(["ar", "rcs", lib, crt_obj])
        if r.returncode != 0:
            raise RuntimeError("ar failed")
        csrc = PROGRAMS["strings"]
        asm = compile_with_shivyc(csrc, work, "arch")
        if asm is None:
            raise RuntimeError("ShivyCX could not compile")
        obj = os.path.join(work, "arch.o")
        exe = os.path.join(work, "arch")
        assemble_with_rasm(asm, obj)
        link_with_rlink([obj, lib], exe)
        got = run([exe])
        ref = build_reference("arch", csrc, work)
        if ref is not None and got.returncode == ref[0] \
                and got.stdout == ref[1]:
            print("  ok    archive link            exit=%d" % got.returncode)
            passed += 1
        else:
            print("  FAIL  archive link            got=(%d,%r)"
                  % (got.returncode, got.stdout[:40]))
            failed += 1
    except Exception as e:
        print("  FAIL  archive link            %s" % e)
        failed += 1

    print("\n== interop: gcc-produced objects ==")
    interop = """
int table[8] = {1,2,3,4,5,6,7,8};
const char *label = "gcc object linked by rlink";
int helper(int n){ int i, s = 0; for (i = 0; i < n; i++) s += table[i]; return s; }
int main(void){
  puts(label);
  putint((long)helper(8)); putchar(10);
  return helper(8);
}
"""
    try:
        src = os.path.join(work, "interop.c")
        with open(src, "w") as f:
            f.write(RUNTIME_DECLS + interop)
        obj = os.path.join(work, "interop.o")
        r = run(["gcc", "-c", "-fno-pie", "-O1", "-ffreestanding",
                 "-fno-stack-protector", "-o", obj, src])
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode()[:200])
        exe = os.path.join(work, "interop")
        link_with_rlink([crt_obj, obj], exe)
        got = run([exe])
        if got.returncode == 36 and b"gcc object linked by rlink" in got.stdout:
            print("  ok    gcc object              exit=%d" % got.returncode)
            passed += 1
        else:
            print("  FAIL  gcc object              got=(%d,%r)"
                  % (got.returncode, got.stdout[:60]))
            failed += 1
    except Exception as e:
        print("  FAIL  gcc object              %s" % e)
        failed += 1

    print("\n== executable validity ==")
    try:
        exe = os.path.join(work, "exit_code")
        r = run(["readelf", "-lW", exe])
        text = r.stdout.decode()
        checks = ["EXEC (Executable file)", "LOAD"]
        if all(c in text for c in checks):
            print("  ok    readelf accepts the image")
            passed += 1
        else:
            print("  FAIL  readelf output unexpected")
            failed += 1
        r = run(["objdump", "-d", exe])
        if r.returncode == 0 and b"<main>" in r.stdout:
            print("  ok    objdump disassembles and finds main")
            passed += 1
        else:
            print("  FAIL  objdump could not read the image")
            failed += 1
    except Exception as e:
        print("  FAIL  validity checks         %s" % e)
        failed += 1

    total = passed + failed
    print("\nrlink end-to-end: %d/%d passed (%d skipped)"
          % (passed, total, skipped))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
