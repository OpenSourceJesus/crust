"""Tests for the FreeBSD/amd64 target: `--os freebsd`.

FreeBSD/amd64 shares Linux's calling convention, data model and object
format, so what these pin down is everything around the code: the system's
toolchain (no `as` -- LLVM's assembler, via cc -- and lld), its libc, the
kernel's ELF branding, and the headers and macros portable code tests.

Tiers, each running where its tools are:

1. Pure Python, anywhere: OS selection, predefined macros, the bundled
   headers' FreeBSD branches, the assembler dialect (the three spellings
   LLVM's assembler rejects), and the driver's diagnostics.
2. LLVM's assembler (`clang`): every feature test's output assembles with
   `clang --target=x86_64-unknown-freebsd`, which is what FreeBSD's cc runs.
   Guards the dialect with no FreeBSD at all.
3. A FreeBSD sysroot (CRUST_FREEBSD_SYSROOT, plus clang and ld.lld): a full
   cross build from this host, checked for the kernel's branding and the
   FreeBSD dynamic loader.
4. A FreeBSD machine over SSH (CRUST_FREEBSD_SSH=user@host, optional
   CRUST_FREEBSD_SSH_PORT and CRUST_FREEBSD_SSH_PASSWORD for sshpass):
   programs run there, against FreeBSD's own cc building the same source.
5. Crust itself running on that machine, when it has `python3`.

The compiler always runs as a subprocess, as in the other target tests.
"""

import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEATURE_DIR = os.path.join(ROOT, "tests", "feature_tests")


def _env(**extra):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra)
    return env


def _crust(args, cwd, **env):
    return subprocess.run([sys.executable, "-m", "shivyc.main"] + args,
                          cwd=cwd, env=_env(**env), capture_output=True,
                          text=True, timeout=600)


def _asm(src, extra=()):
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write(src)
        r = _crust(["--os", "freebsd", "-S", "t.c", "-o", "t.s"]
                   + list(extra), d)
        if r.returncode != 0:
            raise AssertionError("compile failed:\n" + r.stdout + r.stderr)
        with open(os.path.join(d, "t.s")) as f:
            return f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _ret(expr, headers="", extra=()):
    """The constant `return (int)(expr);` compiles to, for FreeBSD."""
    a = _asm(headers + "int f(void) { return (int)(%s); }\n" % expr, extra)
    body = a[a.index("\nf:\n"):]
    return int(re.findall(r"mov eax, (-?\d+)", body)[-1])


def _expected_return(path):
    want = 0
    with open(path) as fh:
        for line in fh:
            if line.strip().startswith("// Return:"):
                want = int(line.split("// Return:")[-1])
    return want


# ==========================================================================
# Tier 1: pure Python
# ==========================================================================
class OSSelectionTests(unittest.TestCase):
    def test_aliases_and_target(self):
        from shivyc.targets import normalize_os, get_target, is_supported_os
        self.assertEqual(normalize_os("FreeBSD"), "freebsd")
        self.assertEqual(normalize_os("fbsd"), "freebsd")
        t = get_target("x86_64", "freebsd")
        self.assertEqual((t.os, t.abi, t.obj_format, t.exe_format),
                         ("freebsd", "sysv", "elf", "elf"))
        self.assertFalse(is_supported_os("arm64", "freebsd"))

    def test_refusals(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write("int main(void){return 0;}\n")
        r = _crust(["--target", "arm64", "--os", "freebsd", "-S", "t.c"], d)
        self.assertIn("FreeBSD on x86_64 only", r.stdout + r.stderr)
        r = _crust(["--os", "freebsd", "-S", "t.c", "--musl"], d)
        self.assertIn("--musl cannot target FreeBSD", r.stdout + r.stderr)
        r = _crust(["--os", "freebsd", "t.c"], d, SHIVYC_RLINK="1")
        self.assertIn("SHIVYC_RLINK cannot target FreeBSD",
                      r.stdout + r.stderr)


class PredefinedMacroTests(unittest.TestCase):
    """As FreeBSD's cc predefines them (checked with `cc -dM -E`)."""

    def test_platform_macros(self):
        self.assertEqual(_ret("__unix__ + __unix * 2 + __ELF__ * 4 + "
                              "__LP64__ * 8 + __x86_64__ * 16 + "
                              "__amd64__ * 32"), 63)

    def test_freebsd_major_defaults_and_overrides(self):
        if sys.platform.startswith("freebsd"):
            self.skipTest("on FreeBSD the host's release is used")
        self.assertEqual(_ret("__FreeBSD__"), 14)
        self.assertEqual(_ret("__FreeBSD__", extra=["-D__FreeBSD__=15"]), 15)

    def test_linux_does_not_claim_freebsd(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write("#ifdef __FreeBSD__\n#error no\n#endif\n"
                    "int main(void){return 0;}\n")
        r = _crust(["--os", "linux", "-S", "t.c"], d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class BundledHeaderTests(unittest.TestCase):
    def test_standard_streams_are_bsd_names(self):
        a = _asm("#include <stdio.h>\n"
                 "int main(void) { fputs(\"x\", stdout); return 0; }\n")
        self.assertIn("__stdoutp", a)
        self.assertNotRegex(a, r"\bstdout\b")

    def test_classification_macros_need_no_library(self):
        a = _asm("#include <math.h>\n"
                 "int f(double x) { return isnan(x) + isinf(x)"
                 " + isfinite(x) + signbit(x); }\n")
        self.assertNotRegex(a, r"call (isnan|isinf|isfinite|signbit)\b")


class AssemblerDialectTests(unittest.TestCase):
    """The spellings GNU as tolerates and LLVM's assembler rejects. Each
    variant assembles to the same bytes under GNU as; see FREEBSD.md."""

    @classmethod
    def setUpClass(cls):
        cls.asm = _asm("static int counter;\n"
                       "long widen(int x) { return (long)x * counter; }\n")

    def test_comm_has_a_comma(self):
        self.assertRegex(self.asm, r"\t\.comm \S+, \d+")
        self.assertNotRegex(self.asm, r"\t\.comm [^,\s]+ \d+")

    def test_sign_extension_to_64_bits_is_movsxd(self):
        self.assertRegex(self.asm, r"movsxd r\w+, (e\w+|r\d+d|DWORD PTR)")
        self.assertNotRegex(self.asm, r"movsx r\w+, (e\w+|r\d+d|DWORD PTR)")

    def test_file_ends_in_prefixed_att_syntax(self):
        self.assertIn(".att_syntax prefix", self.asm)
        self.assertNotIn(".att_syntax noprefix", self.asm)


class DriverTests(unittest.TestCase):
    def test_compile_only_gives_an_x86_64_object(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write("int f(int x) { return x + 1; }\n")
        r = _crust(["--os", "freebsd", "-c", "t.c", "-o", "t.o"], d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(d, "t.o"), "rb") as f:
            hdr = f.read(20)
        self.assertEqual(hdr[:4], b"\x7fELF")
        self.assertEqual(struct.unpack_from("<HH", hdr, 16), (1, 62))

    @unittest.skipIf(sys.platform.startswith("freebsd"), "links natively")
    def test_linking_elsewhere_explains_what_is_needed(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write("int main(void){return 0;}\n")
        env = {"CRUST_FREEBSD_SYSROOT": ""}
        r = _crust(["--os", "freebsd", "t.c", "-o", "t"], d, **env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("CRUST_FREEBSD_SYSROOT", r.stdout + r.stderr)


# ==========================================================================
# Tier 2: LLVM's assembler
# ==========================================================================
def _clang():
    return shutil.which("clang")


def _corpus():
    """(name, [sources]) for every runnable feature test."""
    files = sorted(glob.glob(os.path.join(FEATURE_DIR, "*.c")))
    names = set(files)
    out = []
    for f in files:
        b = os.path.basename(f)[:-2]
        if b.startswith("error_") or b.endswith("_helper"):
            continue
        helper = f[:-2] + "_helper.c"
        out.append((b, [f] + ([helper] if helper in names else [])))
    return out


@unittest.skipUnless(_clang(), "needs clang (LLVM's assembler)")
class LLVMAssemblerTests(unittest.TestCase):
    def test_feature_corpus_assembles(self):
        work = tempfile.mkdtemp()
        for h in glob.glob(os.path.join(FEATURE_DIR, "*.h")):
            shutil.copy(h, work)
        for name, srcs in _corpus():
            for s in srcs:
                with self.subTest(test=os.path.basename(s)):
                    shutil.copy(s, work)
                    b = os.path.basename(s)
                    r = _crust(["--os", "freebsd", "-S", b, "-o",
                                b[:-2] + ".s"], work)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    a = subprocess.run(
                        [_clang(), "--target=x86_64-unknown-freebsd", "-c",
                         b[:-2] + ".s", "-o", b[:-2] + ".o"],
                        cwd=work, capture_output=True, text=True)
                    self.assertEqual(a.returncode, 0, a.stderr)


# ==========================================================================
# Tier 3: cross-link against a FreeBSD sysroot
# ==========================================================================
def _sysroot():
    s = os.environ.get("CRUST_FREEBSD_SYSROOT")
    ok = (s and os.path.isdir(s) and _clang() and shutil.which("ld.lld"))
    return s if ok else None


HELLO = r'''
#include <stdio.h>
int main(int argc, char **argv) {
    printf("hello %d %s\n", argc, argc > 1 ? argv[1] : "-");
    return 42;
}
'''


@unittest.skipUnless(_sysroot(), "needs CRUST_FREEBSD_SYSROOT, clang, ld.lld")
class SysrootLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        with open(os.path.join(cls.d, "hello.c"), "w") as f:
            f.write(HELLO)
        r = _crust(["--os", "freebsd", "hello.c", "-o", "hello"], cls.d)
        if r.returncode != 0:
            raise AssertionError(r.stdout + r.stderr)
        with open(os.path.join(cls.d, "hello"), "rb") as f:
            cls.elf = f.read()

    def test_branded_freebsd(self):
        # The kernel refuses an unbranded ELF (kern.elf64.fallback_brand is
        # -1): EI_OSABI must be ELFOSABI_FREEBSD.
        self.assertEqual(self.elf[:4], b"\x7fELF")
        self.assertEqual(self.elf[7], 9)
        self.assertEqual(struct.unpack_from("<H", self.elf, 16)[0], 2)

    def test_interpreter_is_freebsds(self):
        phoff = struct.unpack_from("<Q", self.elf, 32)[0]
        phentsize, phnum = struct.unpack_from("<HH", self.elf, 54)
        interp = None
        for i in range(phnum):
            p = phoff + i * phentsize
            if struct.unpack_from("<I", self.elf, p)[0] == 3:    # PT_INTERP
                off = struct.unpack_from("<Q", self.elf, p + 8)[0]
                sz = struct.unpack_from("<Q", self.elf, p + 32)[0]
                interp = self.elf[off:off + sz].rstrip(b"\0")
        self.assertEqual(interp, b"/libexec/ld-elf.so.1")


# ==========================================================================
# Tier 4: a FreeBSD machine
# ==========================================================================
def _ssh_target():
    return os.environ.get("CRUST_FREEBSD_SSH")


def _ssh_base(tool):
    port = os.environ.get("CRUST_FREEBSD_SSH_PORT", "22")
    opts = ["-o", "StrictHostKeyChecking=no", "-o",
            "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
    pw = os.environ.get("CRUST_FREEBSD_SSH_PASSWORD")
    pre = ["sshpass", "-p", pw] if pw else []
    if tool == "ssh":
        return pre + ["ssh"] + opts + ["-p", port]
    return pre + ["scp", "-q"] + opts + ["-P", port]


def _remote(cmd, timeout=900):
    return subprocess.run(_ssh_base("ssh") + [_ssh_target(), cmd],
                          capture_output=True, text=True, timeout=timeout)


def _push(paths, dest):
    subprocess.run(_ssh_base("scp") + list(paths)
                   + ["%s:%s" % (_ssh_target(), dest)], check=True,
                   timeout=900)


RUNTIME = r'''
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <setjmp.h>
#include <math.h>
#include <ctype.h>
static jmp_buf jb;
static int depth(int n) { if (n == 0) longjmp(jb, 7); return depth(n - 1) + 1; }
static int cmp(const void *a, const void *b) { return *(const int *)a - *(const int *)b; }
int main(int argc, char **argv) {
    char b[16]; int v[5] = {4, 1, 5, 2, 3}, r; float f = 1.5f;
    int n = snprintf(b, 8, "%s-%d", "truncate", 12345);
    printf("snprintf %d [%s] %d\n", n, b, (int)strlen(b));
    qsort(v, 5, sizeof(int), cmp);
    printf("qsort %d %d %d %d %d\n", v[0], v[1], v[2], v[3], v[4]);
    printf("floats %.2f %.3f %g %.4f\n", f, 2.0 / 3.0, 1e10, sqrt(2.0));
    r = setjmp(jb); if (r == 0) depth(50);
    printf("longjmp %d long=%d isnan=%d upper=%c\n", r, (int)sizeof(long),
           isnan(0.0 / 0.0) != 0, toupper('q'));
    fprintf(stderr, "stderr ok\n"); fflush(stdout);
    printf("argc=%d argv1=%s\n", argc, argc > 1 ? argv[1] : "-");
    return 3;
}
'''

# Feature tests whose FreeBSD cc build is not a usable oracle, each for a
# reason in the test itself rather than in either compiler:
UNDEFINED_BEHAVIOUR = {
    "pointer_math",   # compares addresses of separate locals (stack layout)
    "include",        # strcpy into a string literal (clang's is read-only)
}
# ...and one that cannot link on FreeBSD at all: it declares glibc's
# `extern void *stdout`, where FreeBSD's libc has only __stdoutp.
FREEBSD_LINK_ERROR = {"storage"}


@unittest.skipUnless(_ssh_target(), "set CRUST_FREEBSD_SSH to a FreeBSD host")
class FreeBSDRunTests(unittest.TestCase):
    """Crust's objects linked by FreeBSD's cc -- the command the driver runs
    on a FreeBSD host (freebsd_link_cmd) -- and run beside cc's own build."""

    @classmethod
    def setUpClass(cls):
        cls.rdir = "/tmp/crust-fbsd-%d" % os.getpid()
        r = _remote("rm -rf %s && mkdir -p %s && uname -s"
                    % (cls.rdir, cls.rdir))
        if r.stdout.strip() != "FreeBSD":
            raise AssertionError("not a FreeBSD host: %r" % (r.stdout
                                                             + r.stderr))

    def _obj(self, src, work, name):
        with open(os.path.join(work, name + ".c"), "w") as f:
            f.write(src)
        r = _crust(["--os", "freebsd", "-c", name + ".c", "-o",
                    name + ".o"], work)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_runtime_matches_freebsd_cc(self):
        work = tempfile.mkdtemp()
        self._obj(RUNTIME, work, "rt")
        _push([os.path.join(work, "rt.o"), os.path.join(work, "rt.c")],
              self.rdir)
        r = _remote("cd %s && cc rt.o -lm -o rt && cc -w rt.c -lm -o ref && "
                    "./rt hello > got 2>&1; g=$?; ./ref hello > exp 2>&1; "
                    "e=$?; echo $g $e; cmp -s got exp && echo SAME; cat got"
                    % self.rdir)
        lines = r.stdout.split("\n")
        self.assertEqual(lines[0], "3 3", r.stdout + r.stderr)
        self.assertEqual(lines[1], "SAME", r.stdout)

    def test_feature_corpus_matches_freebsd_cc(self):
        work = tempfile.mkdtemp()
        for h in glob.glob(os.path.join(FEATURE_DIR, "*.h")):
            shutil.copy(h, work)
        script = ["cd %s/corpus" % self.rdir]
        expect = {}
        for name, srcs in _corpus():
            objs = []
            for s in srcs:
                shutil.copy(s, work)
                b = os.path.basename(s)[:-2]
                r = _crust(["--os", "freebsd", "-c", b + ".c", "-o",
                            b + ".o"], work)
                self.assertEqual(r.returncode, 0, (name, r.stdout + r.stderr))
                objs.append(b + ".o")
            expect[name] = _expected_return(srcs[0])
            cs = " ".join(os.path.basename(s) for s in srcs)
            script.append(
                "if cc %s -lm -o %s.crust 2>/dev/null; then ./%s.crust "
                ">/dev/null 2>&1; c=$?; else c=link; fi; if cc -w -O0 %s -o "
                "%s.ref 2>/dev/null; then ./%s.ref >/dev/null 2>&1; r=$?; "
                "else r=none; fi; echo %s $c $r"
                % (" ".join(objs), name, name, cs, name, name, name))
        with open(os.path.join(work, "run.sh"), "w") as f:
            f.write("\n".join(script) + "\n")
        tgz = os.path.join(tempfile.mkdtemp(), "corpus.tgz")
        with tarfile.open(tgz, "w:gz") as t:
            t.add(work, arcname="corpus")
        _push([tgz], self.rdir)
        r = _remote("cd %s && tar -xzf corpus.tgz && sh corpus/run.sh"
                    % self.rdir, timeout=1800)
        results = {}
        for line in r.stdout.strip().split("\n"):
            n, c, o = line.split()
            results[n] = (c, o)
        self.assertEqual(set(results), set(expect))
        for name, (crust, oracle) in sorted(results.items()):
            with self.subTest(test=name):
                if name in FREEBSD_LINK_ERROR:
                    self.assertEqual(crust, "link")
                elif oracle == "none" or name in UNDEFINED_BEHAVIOUR:
                    self.assertEqual(crust, str(expect[name]))
                else:
                    self.assertEqual(crust, oracle)


@unittest.skipUnless(_ssh_target() and _sysroot(),
                     "needs CRUST_FREEBSD_SSH and CRUST_FREEBSD_SYSROOT")
class CrossBuiltRunTests(unittest.TestCase):
    """A binary compiled *and linked* on this host runs on FreeBSD."""

    def test_cross_built_hello(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "hello.c"), "w") as f:
            f.write(HELLO)
        r = _crust(["--os", "freebsd", "hello.c", "-o", "hello"], d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rdir = "/tmp/crust-fbsd-x%d" % os.getpid()
        _remote("mkdir -p " + rdir)
        _push([os.path.join(d, "hello")], rdir)
        r = _remote("cd %s && ./hello cross; echo exit=$?" % rdir)
        self.assertEqual(r.stdout, "hello 2 cross\nexit=42\n")


# ==========================================================================
# Tier 5: Crust running on FreeBSD
# ==========================================================================
def _remote_python():
    if not _ssh_target():
        return None
    try:
        r = _remote("command -v python3", timeout=60)
    except Exception:
        return None
    p = r.stdout.strip()
    return p or None


@unittest.skipUnless(_remote_python(),
                     "needs CRUST_FREEBSD_SSH to a host with python3")
class FreeBSDHostTests(unittest.TestCase):
    """`python3 crust hello.c` on FreeBSD: no --os, the host's cc."""

    def test_native_build(self):
        files = subprocess.run(["git", "ls-files"], cwd=ROOT,
                               capture_output=True, text=True).stdout.split()
        tgz = os.path.join(tempfile.mkdtemp(), "crust.tgz")
        with tarfile.open(tgz, "w:gz") as t:
            for f in files:
                if os.path.isfile(os.path.join(ROOT, f)):
                    t.add(os.path.join(ROOT, f), arcname="crust/" + f)
        rdir = "/tmp/crust-fbsd-h%d" % os.getpid()
        _remote("mkdir -p " + rdir)
        _push([tgz], rdir)
        with open(os.path.join(os.path.dirname(tgz), "hello.c"), "w") as f:
            f.write(HELLO)
        _push([os.path.join(os.path.dirname(tgz), "hello.c")], rdir)
        r = _remote("cd %s && tar -xzf crust.tgz && python3 crust/crust "
                    "hello.c -o hello && ./hello native; echo exit=$?"
                    % rdir, timeout=1800)
        self.assertTrue(r.stdout.endswith("hello 2 native\nexit=42\n"),
                        r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
