"""Tests for the 64-bit Windows target: `--os windows` (x86-64).

Three tiers, so the target is covered wherever the suite runs:

1. Pure Python. Text checks on the emitted assembly (`-S`) pin the Microsoft
   x64 calling convention where it differs from System V -- the differences
   that miscompile silently -- and a full compile + assemble + link produces
   a PE image that is parsed here and checked field by field. Crust's own
   assembler and linker are Python, so this needs nothing installed.
2. Run under Wine (`apt install wine64`): the programs execute, including
   the whole feature_tests corpus.
3. Wine plus MinGW-w64 gcc (`apt install gcc-mingw-w64-x86-64`), used as the
   reference implementation of the ABI: tests/windows_abi/ is built by both
   compilers and every caller/callee pairing is run through a DLL boundary.
   Nothing of MinGW ends up in a Crust build; it is only the oracle.
4. Crust itself running on Windows, when CRUST_WINDOWS_PYTHON names a
   Windows python.exe (run under Wine off Windows). This is the case the port
   exists for -- Python and Crust, nothing else -- and it exercises what cross
   compilation cannot: host detection, Windows paths, and the child process
   that builds the runtime.

The compiler always runs as a subprocess: the ABI and data model are module
state, and a Windows compile must not leak into the tests sharing this
interpreter.
"""

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ABI_DIR = os.path.join(ROOT, "tests", "windows_abi")
FEATURE_DIR = os.path.join(ROOT, "tests", "feature_tests")


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["WINEDEBUG"] = "-all"
    return env


def _crust(args, cwd):
    return subprocess.run([sys.executable, "-m", "shivyc.main"] + args,
                          cwd=cwd, env=_env(), capture_output=True, text=True,
                          timeout=600)


def _asm(src, extra=()):
    """Compile `src` for Windows with -S and return the assembly text."""
    d = tempfile.mkdtemp()
    try:
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write(src)
        r = _crust(["--os", "windows", "-S", "t.c", "-o", "t.s"] + list(extra),
                   d)
        if r.returncode != 0:
            raise AssertionError("compile failed:\n" + r.stdout + r.stderr)
        with open(os.path.join(d, "t.s")) as f:
            return f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _body(asm, name):
    """The lines of function `name` in `asm`, up to the next global label."""
    lines = asm.split("\n")
    out, on = [], False
    for ln in lines:
        if ln == name + ":":
            on = True
            continue
        if on and re.match(r"^[A-Za-z_]\w*:$", ln) \
                and not ln.startswith("__shivyc_label"):
            break
        if on:
            out.append(ln.strip())
    return out


def _link(src_files, workdir, extra=()):
    """Compile and link for Windows; return (exe path or None, result)."""
    exe = os.path.join(workdir, "t.exe")
    r = _crust(["--os", "windows"] + list(src_files) + ["-o", exe]
               + list(extra), workdir)
    return (exe if r.returncode == 0 and os.path.exists(exe) else None), r


# --------------------------------------------------------------------------
# A small PE reader, independent of rlink_pe, for checking its output.
# --------------------------------------------------------------------------
class PE:
    def __init__(self, data):
        self.d = data
        self.pe = struct.unpack_from("<I", data, 0x3C)[0]
        self.machine, self.nsec = struct.unpack_from("<HH", data, self.pe + 4)
        self.chars = struct.unpack_from("<H", data, self.pe + 22)[0]
        o = self.pe + 24
        self.opt = o
        self.magic = struct.unpack_from("<H", data, o)[0]
        self.entry = struct.unpack_from("<I", data, o + 16)[0]
        self.base = struct.unpack_from("<Q", data, o + 24)[0]
        self.sect_align, self.file_align = struct.unpack_from("<II", data,
                                                              o + 32)
        self.image_size, self.headers_size = struct.unpack_from("<II", data,
                                                                o + 56)
        self.subsystem, self.dllchars = struct.unpack_from("<HH", data, o + 68)
        self.dirs = [struct.unpack_from("<II", data, o + 112 + 8 * i)
                     for i in range(16)]
        self.sections = []
        for i in range(self.nsec):
            s = o + 240 + 40 * i
            name = data[s:s + 8].rstrip(b"\0").decode()
            vsz, va, rsz, raw = struct.unpack_from("<IIII", data, s + 8)
            ch = struct.unpack_from("<I", data, s + 36)[0]
            self.sections.append((name, va, vsz, raw, rsz, ch))

    def section_of(self, rva):
        for sec in self.sections:
            if sec[1] <= rva < sec[1] + max(sec[2], sec[4]):
                return sec
        return None

    def off(self, rva):
        sec = self.section_of(rva)
        return sec[3] + rva - sec[1]

    def cstr(self, rva):
        o = self.off(rva)
        return self.d[o:self.d.index(b"\0", o)].decode()

    def imports(self):
        """{dll: {name: iat_rva}} from the import directory."""
        out = {}
        rva = self.dirs[1][0]
        while True:
            ilt, _, _, name, iat = struct.unpack_from("<IIIII", self.d,
                                                      self.off(rva))
            if ilt == 0 and name == 0:
                break
            names = {}
            k = 0
            while True:
                e = struct.unpack_from("<Q", self.d, self.off(ilt) + 8 * k)[0]
                if e == 0:
                    break
                names[self.cstr(e + 2)] = iat + 8 * k
                k += 1
            out[self.cstr(name)] = names
            rva += 20
        return out


# ==========================================================================
# Tier 1: pure Python
# ==========================================================================
class OSSelectionTests(unittest.TestCase):
    def test_aliases(self):
        from shivyc.targets import normalize_os, get_target
        for a in ("windows", "Windows", "win64", "win", "mingw", "win32"):
            self.assertEqual(normalize_os(a), "windows", a)
        t = get_target("x86_64", "windows")
        self.assertEqual((t.os, t.abi, t.exe_format, t.obj_format),
                         ("windows", "win64", "pe", "elf"))
        self.assertEqual(get_target("x86_64", "").abi, "sysv")

    def test_windows_is_x64_only(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write("int main(void){return 0;}\n")
        r = _crust(["--target", "arm64", "--os", "windows", "-S", "t.c"], d)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Windows on x86_64 only", r.stdout + r.stderr)

    def test_system_v_only_options_are_refused_by_name(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write("int main(void){return 0;}\n")
        for flag in ("--musl", "-f-pack-args", "-fmetamorphic",
                     "-fsimd-pack-globals", "-O4", "-rdynamic"):
            r = _crust(["--os", "windows", "-S", "t.c", flag], d)
            self.assertNotEqual(r.returncode, 0, flag)
            self.assertIn("cannot target Windows", r.stdout + r.stderr, flag)


class Win64CallingConventionTests(unittest.TestCase):
    """The Microsoft x64 convention in the emitted assembly."""

    SRC = r'''
int five(int a, int b, int c, int d, int e) { return a + b + c + d + e; }
double pos(int a, double b, int c, double d) { return a + b + c + d; }
int callfive(void) { return five(1, 2, 3, 4, 5); }
double callpos(void) { return pos(1, 2.0, 3, 4.0); }
int printf(const char *, ...);
int callvar(double x) { return printf("%f %d", x, 7); }
'''

    @classmethod
    def setUpClass(cls):
        cls.asm = _asm(cls.SRC)

    def test_integer_args_in_rcx_rdx_r8_r9_then_stack(self):
        b = _body(self.asm, "callfive")
        for reg in ("ecx, 1", "edx, 2", "r8d, 3", "r9d, 4"):
            self.assertIn("mov " + reg, b)
        self.assertIn("push 5", b)

    def test_home_space_is_reserved_for_every_call(self):
        b = _body(self.asm, "callfive")
        i = b.index("call five")
        self.assertEqual(b[i - 1 - 4:i].count("sub rsp, 32"), 1, b)
        # 32 home + 8 (one stack arg) + 8 padding back to 16-byte alignment
        self.assertEqual(b[i + 1], "add rsp, 48")
        b = _body(self.asm, "callpos")
        self.assertIn("sub rsp, 32", b)

    def test_fifth_argument_is_read_above_the_home_space(self):
        # return address at rbp+8, home space rbp+16..47, 5th arg at rbp+48
        self.assertIn("[rbp+48]", "\n".join(_body(self.asm, "five")))

    def test_positional_register_assignment(self):
        # pos(int, double, int, double): the doubles take xmm1 and xmm3,
        # the ints ecx and r8d -- one shared position counter.
        body = _body(self.asm, "callpos")
        b = "\n".join(body[:body.index("call pos")])     # the setup only
        self.assertIn("mov ecx, 1", b)
        self.assertIn("mov r8d, 3", b)
        self.assertRegex(b, r"movsd xmm1, ")
        self.assertRegex(b, r"movsd xmm3, ")
        self.assertNotRegex(b, r"xmm0|xmm2|edx|r9")

    def test_variadic_floats_are_duplicated_without_sysv_extras(self):
        b = "\n".join(_body(self.asm, "callvar"))
        self.assertIn("movq rdx, xmm1", b)     # position 1: xmm1 and rdx
        self.assertNotIn("r11", b)             # no System V block hand-off
        self.assertNotIn("mov eax, 1", b)      # no vector count in al

    def test_rsi_and_rdi_are_preserved_when_used(self):
        src = "long long f(long long *v) {\n" + "".join(
            "long long a%d = v[%d];\n" % (i, i) for i in range(14)) + \
            "int i; for (i = 0; i < 9; i++) {" + "".join(
            "a%d += a%d;" % (i, (i + 1) % 14) for i in range(14)) + "}\n" \
            "return " + " + ".join("a%d" % i for i in range(14)) + ";}\n"
        b = _body(_asm(src), "f")
        text = "\n".join(b)
        for reg in ("rsi", "rdi"):
            if re.search(r"\b%s\b" % reg, text):
                self.assertTrue(any(re.match(r"mov QWORD PTR \[rbp-\d+\], %s$"
                                             % reg, x) for x in b), reg)

    def test_variadic_callee_spills_the_home_space(self):
        b = "\n".join(_body(_asm(
            "int v(int n, ...) { char *ap = (char *)__builtin_va_start_addr();"
            " return *(int *)(ap + 8); }\n"), "v"))
        for i, reg in enumerate(("rcx", "rdx", "r8", "r9")):
            self.assertIn("mov QWORD PTR [rbp+%d], %s" % (16 + 8 * i, reg), b)

    def test_struct_by_reference_and_hidden_return_pointer(self):
        src = ("struct B { long long a, b, c; };\n"
               "struct T { char c[3]; };\n"
               "long long take(struct B b) { return b.a + b.c; }\n"
               "struct T mk(int x) { struct T t; t.c[0] = x; return t; }\n"
               "struct S { int x, y; };\n"
               "struct S mk8(int x) { struct S s; s.x = x; s.y = x; return s; }\n"
               "long long call(void) { struct B b; b.a = 1; b.c = 2;"
               " return take(b); }\n")
        a = _asm(src)
        # the callee receives a pointer and copies through it
        self.assertIn("rcx", "\n".join(_body(a, "take")))
        # A 3-byte struct is returned through a hidden pointer: it takes
        # position 0 (rcx), pushing `x` to position 1 (edx), the struct is
        # written through it, and the pointer is handed back in rax. An
        # 8-byte struct comes back in rax itself.
        mk = _body(a, "mk")
        self.assertIn("mov BYTE PTR [rax], dl", mk)
        self.assertTrue(any("PTR [rcx]" in x for x in mk), mk)
        self.assertEqual(mk[mk.index("ret") - 3], "mov rax, rcx")
        self.assertIn("mov rax, QWORD PTR", "\n".join(_body(a, "mk8")))
        # the caller passes the address of a copy, not the struct's words
        c = "\n".join(_body(a, "call"))
        self.assertRegex(c, r"lea rcx, \[rbp-\d+\]")

    def test_inline_asm_respects_callee_saved_registers(self):
        a = _asm(r'''
int f(int x) { int y; __asm__("mov %1, %0" : "=r"(y) : "r"(x)); return y; }
int g(int x) { __asm__ volatile("xor %%esi, %%esi" ::: "rsi"); return x; }
''')
        # operands come from the Win64 scratch registers only
        self.assertNotRegex("\n".join(_body(a, "f")), r"\b[re]?(si|di)\b")
        # a clobbered callee-saved register is saved and restored
        g = _body(a, "g")
        at = g.index("xor %esi, %esi")
        self.assertTrue(any(re.match(r"mov QWORD PTR \[rbp-\d+\], rsi$", x)
                            for x in g[:at]), g)
        self.assertTrue(any(re.match(r"mov rsi, QWORD PTR \[rbp-\d+\]$", x)
                            for x in g[at:]), g)

    def test_no_red_zone(self):
        """Windows may overwrite anything below rsp at any time."""
        a = _asm(self.SRC + "int leaf(int a, int b) { return a * b + a; }\n",
                 ["-fstackless-calls"])
        self.assertNotRegex(a, r"\[rsp-\d+\]")

    def test_large_frames_probe_each_page(self):
        big = _body(_asm("int f(void) { char b[9000]; b[0] = 1;"
                         " return b[0]; }\n"), "f")
        self.assertIn("test DWORD PTR [rsp], esp", big)
        self.assertIn("sub rsp, 4096", big)
        small = _body(_asm("int f(void) { char b[2000]; b[0] = 1;"
                           " return b[0]; }\n"), "f")
        self.assertNotIn("test DWORD PTR [rsp], esp", small)


class LLP64Tests(unittest.TestCase):
    """`long` is 4 bytes on 64-bit Windows; `long long` and pointers 8."""

    def _ret(self, expr, headers=""):
        b = _body(_asm(headers + "int f(void) { return (int)(%s); }\n" % expr),
                  "f")
        m = [x for x in b if x.startswith("mov eax, ")]
        return int(m[-1].split(", ")[1])

    def test_sizes(self):
        self.assertEqual(self._ret("sizeof(long)"), 4)
        self.assertEqual(self._ret("sizeof(unsigned long)"), 4)
        self.assertEqual(self._ret("sizeof(long long)"), 8)
        self.assertEqual(self._ret("sizeof(void *)"), 8)
        self.assertEqual(self._ret("sizeof(1L)"), 4)
        self.assertEqual(self._ret("sizeof(1LL)"), 8)
        self.assertEqual(self._ret("sizeof(3000000000L)"), 8)
        self.assertEqual(self._ret("sizeof(long double)"), 8)

    def test_bundled_headers(self):
        hdr = "#include <stddef.h>\n#include <stdint.h>\n#include <stdio.h>\n"
        self.assertEqual(self._ret("sizeof(size_t)", hdr), 8)
        self.assertEqual(self._ret("sizeof(ptrdiff_t)", hdr), 8)
        self.assertEqual(self._ret("sizeof(int64_t)", hdr), 8)
        self.assertEqual(self._ret("sizeof(uintptr_t)", hdr), 8)
        self.assertEqual(self._ret("sizeof(FILE)", hdr), 48)   # msvcrt _iobuf

    def test_predefined_macros(self):
        self.assertEqual(self._ret("_WIN32 + _WIN64 * 2 + __x86_64__ * 4"), 7)
        self.assertEqual(self._ret("sizeof(__SIZE_TYPE__)"), 8)
        self.assertEqual(self._ret("__SIZEOF_LONG__"), 4)

    def test_linux_is_unchanged(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write("int f(int a, int b) { return a - b; }\n"
                    "int g(void) { return (int)sizeof(long); }\n")
        r = _crust(["-S", "t.c", "-o", "t.s"], d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(d, "t.s")) as fh:
            a = fh.read()
        self.assertIn("edi", "\n".join(_body(a, "f")))
        self.assertIn("mov eax, 8", "\n".join(_body(a, "g")))


HELLO = r'''
#include <stdio.h>
int main(int argc, char **argv) {
    printf("hello %d %s\n", argc, argv[1]);
    return 42;
}
'''


class PEImageTests(unittest.TestCase):
    """Link with rasm + rlink (both Python) and read the PE back."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        with open(os.path.join(cls.d, "hello.c"), "w") as f:
            f.write(HELLO)
        exe, r = _link(["hello.c"], cls.d)
        if exe is None:
            raise AssertionError(r.stdout + r.stderr)
        with open(exe, "rb") as f:
            cls.pe = PE(f.read())

    def test_headers(self):
        pe = self.pe
        self.assertEqual(pe.d[:2], b"MZ")
        self.assertEqual(pe.d[pe.pe:pe.pe + 4], b"PE\0\0")
        self.assertEqual(pe.machine, 0x8664)
        self.assertEqual(pe.magic, 0x20B)                  # PE32+
        self.assertEqual(pe.subsystem, 3)                  # console
        self.assertEqual(pe.base, 0x400000)
        self.assertTrue(pe.chars & 0x0001)                 # relocs stripped
        self.assertTrue(pe.chars & 0x0002)                 # executable
        self.assertFalse(pe.dllchars & 0x0040)             # no DYNAMIC_BASE
        self.assertTrue(pe.dllchars & 0x0100)              # NX compatible

    def test_sections_obey_alignment(self):
        pe = self.pe
        for name, va, vsz, raw, rsz, ch in pe.sections:
            self.assertEqual(va % pe.sect_align, 0, name)
            self.assertEqual(raw % pe.file_align, 0, name)
            self.assertEqual(rsz % pe.file_align, 0, name)
            self.assertLessEqual(va + vsz, pe.image_size, name)
        text = pe.section_of(pe.entry)
        self.assertEqual(text[0], ".text")
        self.assertTrue(text[5] & 0x20000000)              # executable

    def test_imports_come_from_msvcrt(self):
        imp = self.pe.imports()
        self.assertEqual(list(imp), ["msvcrt.dll"])
        for name in ("printf", "exit", "__getmainargs", "__set_app_type"):
            self.assertIn(name, imp["msvcrt.dll"])
        # snprintf's C99 wrapper is only linked when something uses it
        self.assertNotIn("_vscprintf", imp["msvcrt.dll"])

    def test_iat_directory_covers_the_slots(self):
        pe = self.pe
        iat_rva, iat_size = pe.dirs[12]
        for rva in pe.imports()["msvcrt.dll"].values():
            self.assertTrue(iat_rva <= rva < iat_rva + iat_size)

    def test_stub_jumps_through_the_slot(self):
        """Every `jmp [rip+disp]` stub in .text targets an IAT slot."""
        pe = self.pe
        slots = set(pe.imports()["msvcrt.dll"].values())
        text = [s for s in pe.sections if s[0] == ".text"][0]
        body = pe.d[text[3]:text[3] + text[2]]
        targets = set()
        for i in range(len(body) - 5):
            if body[i] == 0xFF and body[i + 1] == 0x25:
                disp = struct.unpack_from("<i", body, i + 2)[0]
                targets.add(text[1] + i + 6 + disp)
        self.assertEqual(targets, slots)


class LinkDiagnosticsTests(unittest.TestCase):
    def test_data_exports_need_the_import_table(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write("extern char **_environ;\n"
                    "int main(void) { return _environ != 0; }\n")
        exe, r = _link(["t.c"], d)
        self.assertIsNone(exe)
        self.assertIn("__imp__environ", r.stdout + r.stderr)

    def test_data_export_via_imp_links(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write("extern char ***__imp__environ;\n"
                    "int main(void) { return **__imp__environ != 0; }\n")
        exe, r = _link(["t.c"], d)
        self.assertIsNotNone(exe, r.stdout + r.stderr)

    def test_undefined_symbol_is_reported(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "t.c"), "w") as f:
            f.write("int nope(void);\nint main(void) { return nope(); }\n")
        exe, r = _link(["t.c"], d)
        self.assertIsNone(exe)
        self.assertIn("undefined reference to: nope", r.stdout + r.stderr)


class ImportTableDataTests(unittest.TestCase):
    def test_lookup(self):
        sys.path.insert(0, os.path.join(ROOT, "tools", "rpy_lib"))
        import win64_imports as w
        self.assertEqual(w.lookup("printf"), ("msvcrt.dll", False))
        self.assertEqual(w.lookup("_iob"), ("msvcrt.dll", True))
        self.assertEqual(w.lookup("sin"), ("msvcrt.dll", False))
        self.assertEqual(w.lookup("ExitProcess"), ("KERNEL32.dll", False))
        self.assertIsNone(w.lookup("snprintf"))     # C99; not in msvcrt.dll


# ==========================================================================
# Tier 2: run under Wine
# ==========================================================================
def _wine():
    for cand in (os.environ.get("WINE"), shutil.which("wine64"),
                 "/usr/lib/wine/wine64", shutil.which("wine")):
        if cand and os.path.exists(cand):
            return cand
    return None


def _run_exe(exe, args=(), cwd=None):
    p = subprocess.run([_wine(), exe] + list(args), env=_env(), cwd=cwd,
                       capture_output=True, timeout=60)
    return p.returncode, p.stdout.decode(errors="replace").replace("\r\n",
                                                                    "\n")


def _build_run(src, extra=(), args=()):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "t.c"), "w") as f:
        f.write(src)
    exe, r = _link(["t.c"], d, extra)
    if exe is None:
        raise AssertionError(r.stdout + r.stderr)
    return _run_exe(exe, args)


RUNTIME = r'''
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <setjmp.h>
static jmp_buf jb;
static int depth(int n) { if (n == 0) longjmp(jb, 7); return depth(n - 1) + 1; }
static int cmp(const void *a, const void *b) { return *(const int *)a - *(const int *)b; }
static int deep(int n) {
    char buf[70000];
    int i;
    buf[0] = (char)n;
    for (i = 4096; i < 70000; i += 4096) buf[i] = (char)(buf[i - 4096] + 1);
    return buf[65536];
}
int main(void) {
    char b[16];
    int v[5] = {4, 1, 5, 2, 3}, r;
    float f = 1.5f;
    int n = snprintf(b, 8, "%s-%d", "truncate", 12345);
    printf("snprintf %d [%s] %d\n", n, b, (int)strlen(b));
    qsort(v, 5, sizeof(int), cmp);
    printf("qsort %d %d %d %d %d\n", v[0], v[1], v[2], v[3], v[4]);
    printf("floats %.2f %.3f %g\n", f, 2.0 / 3.0, 1e10);
    r = setjmp(jb);
    if (r == 0) depth(50);
    printf("longjmp %d\n", r);
    printf("deep %d long %d\n", deep(3), (int)sizeof(long));
    fprintf(stderr, "to stderr\n");
    return 0;
}
'''

# feature_tests whose `// Return:` value assumes LP64 -- each stores a value
# above 2**31 in a `long`, or asks sizeof(long). The Windows value is what
# MinGW-w64 gcc gives for the same source, run the same way; the tier-3 test
# test_lp64_table_matches_mingw re-derives every entry, so this cannot drift.
LP64_DEPENDENT = {"addition": 7, "builtin_bits_inf": 4, "builtin_popcount": 5,
                  "call_imm64_arg": 1, "comparison": 15, "function_call": 5,
                  "implicit_cast": 16, "multiplication": 7, "sizeof": 16,
                  "store_imm64": 1}
# ...and one that declares glibc's `extern void *stdout` by hand, which no
# Windows C library defines: MinGW gcc refuses it at link time too.
WINDOWS_LINK_ERROR = {"storage": "undefined reference to: stdout"}


def _expected_return(path):
    """The `// Return: N` value of a feature test (default 0)."""
    want = 0
    with open(path) as fh:
        for line in fh:
            if line.strip().startswith("// Return:"):
                want = int(line.split("// Return:")[-1])
    return want


@unittest.skipUnless(_wine(), "needs Wine (apt install wine64)")
class WineRunTests(unittest.TestCase):
    def test_hello(self):
        rc, out = _build_run(HELLO, args=["world"])
        self.assertEqual(rc, 42)
        self.assertEqual(out, "hello 2 world\n")

    def test_runtime(self):
        rc, out = _build_run(RUNTIME)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "snprintf 14 [truncat] 7\n"
                              "qsort 1 2 3 4 5\n"
                              "floats 1.50 0.667 1e+010\n"
                              "longjmp 7\n"
                              "deep 19 long 4\n")

    def test_bit_builtins_follow_llp64(self):
        """`l` builtins are 32-bit on Windows; `ll` ones 64 (as MinGW)."""
        rc, out = _build_run(r'''
int printf(const char *, ...);
int main(void) {
    unsigned long long o;
    printf("%d %d %d %d %d %d\n", __builtin_clzl(1UL), __builtin_ctzl(8UL),
           __builtin_clzll(1ULL), __builtin_ctzll(0x100000000ULL),
           __builtin_popcountll(0xFFFFFFFFFFULL),
           __builtin_umulll_overflow(0xFFFFFFFFFFFFFFFFULL, 2ULL, &o));
    return 0;
}
''')
        self.assertEqual((rc, out), (0, "31 3 63 32 40 1\n"))

    def test_feature_corpus(self):
        """Every runnable tests/feature_tests program, as a Windows exe."""
        import glob
        files = sorted(glob.glob(os.path.join(FEATURE_DIR, "*.c")))
        names = set(files)
        work = tempfile.mkdtemp()
        for h in glob.glob(os.path.join(FEATURE_DIR, "*.h")):
            shutil.copy(h, work)
        for f in files:
            base = os.path.basename(f)[:-2]
            if base.startswith("error_") or base.endswith("_helper"):
                continue
            helper = f[:-2] + "_helper.c"
            srcs = [f] + ([helper] if helper in names else [])
            want = LP64_DEPENDENT.get(base, _expected_return(f))
            with self.subTest(test=base):
                for s in srcs:
                    shutil.copy(s, work)
                exe = os.path.join(work, base + ".exe")
                r = _crust(["--os", "windows", "-o", exe]
                           + [os.path.basename(s) for s in srcs], work)
                if base in WINDOWS_LINK_ERROR:
                    self.assertIn(WINDOWS_LINK_ERROR[base],
                                  r.stdout + r.stderr)
                    continue
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(_run_exe(exe)[0], want)


# ==========================================================================
# Tier 3: Wine + MinGW-w64 gcc as the ABI reference
# ==========================================================================
def _mingw():
    return shutil.which("x86_64-w64-mingw32-gcc")


@unittest.skipUnless(_wine() and _mingw(),
                     "needs Wine and MinGW-w64 gcc (the reference ABI)")
class Win64ABIExecutionTests(unittest.TestCase):
    """tests/windows_abi/ run in every caller/callee pairing across a DLL.

    abi_gcc.dll holds gcc's build of the callees and checks (and a register
    preservation probe written in assembly); the Crust executable links to it
    with `-labi_gcc`, which rlink resolves by reading the DLL's export table.
    """

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        for f in os.listdir(ABI_DIR):
            shutil.copy(os.path.join(ABI_DIR, f), cls.d)
        g = subprocess.run([_mingw(), "-shared", "-O1", "-DPFX=g_",
                            "abi_funcs.c", "abi_checks.c", "abi_preserve.S",
                            "-o", "abi_gcc.dll"], cwd=cls.d,
                           capture_output=True, text=True)
        if g.returncode != 0:
            raise AssertionError(g.stderr)
        exe, r = _link(["abi_funcs.c", "abi_checks.c", "abi_main.c"], cls.d,
                       ["-DPFX=c_", "-L.", "-labi_gcc"])
        if exe is None:
            raise AssertionError(r.stdout + r.stderr)
        cls.rc, out = _run_exe(exe, cwd=cls.d)
        cls.results = {}
        for line in out.strip().split("\n"):
            k, v = line.rsplit(" ", 1)
            cls.results[k] = int(v)

    def test_lp64_table_matches_mingw(self):
        """Every LP64_DEPENDENT value is what MinGW gcc actually returns,
        and every other runnable feature test returns its `// Return:`
        value under MinGW too -- so the table is complete as well as right."""
        import glob
        work = tempfile.mkdtemp()
        for h in glob.glob(os.path.join(FEATURE_DIR, "*.h")):
            shutil.copy(h, work)
        files = sorted(glob.glob(os.path.join(FEATURE_DIR, "*.c")))
        names = set(files)
        for f in files:
            base = os.path.basename(f)[:-2]
            if (base.startswith("error_") or base.endswith("_helper")
                    or base in WINDOWS_LINK_ERROR):
                continue
            helper = f[:-2] + "_helper.c"
            srcs = [f] + ([helper] if helper in names else [])
            exe = os.path.join(work, base + ".mgw.exe")
            g = subprocess.run([_mingw(), "-w", "-O0", "-o", exe] + srcs,
                               cwd=work, capture_output=True)
            if g.returncode != 0:
                continue                # Crust-only syntax; no oracle
            with self.subTest(test=base):
                self.assertEqual(
                    _run_exe(exe)[0],
                    LP64_DEPENDENT.get(base, _expected_return(f)))

    def _check(self, key):
        self.assertIn(key, self.results)
        self.assertEqual(self.results[key], 0,
                         "%s: failed check bits 0x%x (see abi_checks.c)"
                         % (key, self.results[key]))

    def test_crust_calls_crust(self):
        self._check("crust->crust")

    def test_crust_calls_gcc(self):
        self._check("crust->gcc")

    def test_gcc_calls_crust(self):
        self._check("gcc->crust")

    def test_gcc_calls_gcc_control(self):
        self._check("gcc->gcc")

    def test_direct_calls_through_import_stubs(self):
        self._check("direct")

    def test_callee_saved_registers_survive_crust(self):
        self._check("preserve")

    def test_exit_status(self):
        self.assertEqual(self.rc, 0)


# ==========================================================================
# Tier 4: Crust running on Windows
# ==========================================================================
def _win_python():
    p = os.environ.get("CRUST_WINDOWS_PYTHON")
    return p if p and os.path.exists(p) else None


def _win_path(p):
    """How a Windows process under Wine names Unix path `p` (drive Z:)."""
    return "Z:" + os.path.abspath(p).replace("/", "\\")


@unittest.skipUnless(_win_python() and (_wine() or os.name == "nt"),
                     "set CRUST_WINDOWS_PYTHON to a Windows python.exe")
class WindowsHostTests(unittest.TestCase):
    """`python crust hello.c` on Windows, with no --os and nothing installed
    but Python. The runtime cache is pointed at a fresh file so its build --
    a child compiler process -- runs every time."""

    def test_native_build(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "hello.c"), "w") as f:
            f.write(HELLO)
        env = _env()
        env["PYTHONPATH"] = _win_path(ROOT)
        env["SHIVYC_RLIBC_WIN64_OBJ"] = _win_path(os.path.join(d, "rt.o"))
        cmd = [_win_python(), "-m", "shivyc.main", "hello.c", "-o",
               "hello.exe"]
        if os.name != "nt":
            cmd = [_wine()] + cmd
        # Windows Python under Wine needs real pipes for its std streams.
        r = subprocess.run(cmd, cwd=d, env=env, capture_output=True,
                           stdin=subprocess.PIPE, timeout=600)
        self.assertEqual(r.returncode, 0, (r.stdout + r.stderr).decode(
            errors="replace"))
        self.assertTrue(os.path.exists(os.path.join(d, "rt.o")))
        rc, out = _run_exe(os.path.join(d, "hello.exe"), ["host"], cwd=d)
        self.assertEqual((rc, out), (42, "hello 2 host\n"))


if __name__ == "__main__":
    unittest.main()
