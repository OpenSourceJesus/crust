"""Tests for the macOS (Apple Silicon) target: `--target arm64 --os macos`.

Three tiers, so the target is covered wherever the suite runs:

1. Text checks on the emitted assembly (`-S`). Need nothing but Python, so
   they run on Linux CI. They pin the Mach-O dialect and Apple's arm64 ABI
   facts that differ from ELF/AAPCS64 -- the ones that miscompile silently.
2. Assemble + link as real Mach-O, when LLVM is installed (clang with the
   arm64-apple target, and ld64.lld). No macOS SDK is needed: the link runs
   against a generated text-based stub of libSystem listing exactly the
   symbols the object imports, the same .tbd format the real SDK ships.
3. Build and *run*, only on an Apple Silicon Mac, with the system toolchain.

Most programs declare their libc prototypes by hand, to keep them about
code generation; FPCLASS includes <stdio.h> and <math.h>, which resolve to
Crust's bundled headers (never the SDK's, unless given -I).

The compiler runs as a subprocess, never in-process: object-format symbol
spelling is module state, and a Mach-O compile must not leak `_` prefixes
into the other tests sharing this interpreter.
"""

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROTOS = ("int printf(const char *, ...);\n"
          "void qsort(void *, unsigned long, unsigned long,\n"
          "           int (*)(const void *, const void *));\n"
          "int strcmp(const char *, const char *);\n")

HELLO = PROTOS + 'int main(void){ printf("hello crust\\n"); return 0; }\n'

# Everything the Mach-O work touched, in one program: a Crust variadic
# callee, a variadic libc call with mixed anonymous types, each kind of
# global, extern data, an external function's address, 2-byte data, float
# literals, loops (compiler labels). Output is deterministic for tier 3.
STRESS = PROTOS + r'''
int counter;
static int hidden;
int initialized = 42;
static short shorts[3] = {1, -2, 3};
const char *greeting = "hi";
extern char **environ;

static int sum(int n, ...) {
    __builtin_va_list ap;
    int t = 0, i;
    __builtin_va_start(ap, n);
    for (i = 0; i < n; i++) t += __builtin_va_arg(ap, int);
    __builtin_va_end(ap);
    return t;
}

static int cmp(const void *a, const void *b) {
    return *(const int *)a - *(const int *)b;
}

int main(void) {
    int v[4] = {3, 1, 4, 1};
    int (*pc)(const char *, const char *) = strcmp;
    int (*pcmp)(const void *, const void *) = cmp;
    double d = 2.5;
    counter = 7; hidden = 5;
    qsort(v, 4, sizeof(int), pcmp);
    printf("%d %d %d %d\n", v[0], v[1], v[2], v[3]);
    printf("sum=%d mixed=%s %d %.2f %ld\n", sum(3, 10, 20, 12),
           greeting, initialized, d * 1.5, (long)counter + hidden);
    printf("shorts=%d %d %d cmp=%d env=%d\n", shorts[0], shorts[1],
           shorts[2], pc("a", "b") < 0, environ != 0);
    return 0;
}
'''
# Uses the *bundled* <stdio.h>/<math.h> (Crust never reads the system's
# headers unless given -I). Exit 0 iff all ten classification checks hold,
# including -0.0's sign and single evaluation of the argument.
FPCLASS = r'''
#include <stdio.h>
#include <math.h>
static int calls;
static double next(void) { calls++; return 1.0 / 0.0; }
int main(void) {
    double z = 0.0, n = z / z, i = 1.0 / z, m = -0.0;
    int r = isnan(n) * 1 + !isnan(i) * 2 + isinf(i) * 4 + !isinf(n) * 8
          + isfinite(z) * 16 + !isfinite(i) * 32 + signbit(m) * 64
          + !signbit(z) * 128 + isinf(next()) * 256 + (calls == 1) * 512;
    fprintf(stderr, "r=%d\n", r);
    return r == 1023 ? 0 : 1;
}
'''

STRESS_OUT = ("1 1 3 4\n"
              "sum=42 mixed=hi 42 3.75 12\n"
              "shorts=1 -2 3 cmp=1 env=1\n")


def _compile(src, extra, out_name="t.s"):
    """Run the compiler on `src` with `extra` args. Returns (rc, text, path)
    where text is the output file (or the diagnostics when rc != 0)."""
    d = tempfile.mkdtemp()
    c = os.path.join(d, "t.c")
    out = os.path.join(d, out_name)
    with open(c, "w") as f:
        f.write(src)
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    for k in ("SHIVYC_RASM", "SHIVYC_RLINK"):
        env.pop(k, None)
    p = subprocess.run([sys.executable, "-m", "shivyc.main", c, "-o", out]
                       + extra, capture_output=True, text=True, cwd=d,
                       env=env)
    if p.returncode != 0 or not os.path.exists(out):
        return p.returncode or 1, p.stdout + p.stderr, out
    if out.endswith(".s"):
        with open(out) as f:
            return 0, f.read(), out
    return 0, "", out


def _macos_asm(src):
    rc, text, path = _compile(src, ["-S", "--target", "arm64",
                                    "--os", "macos"])
    assert rc == 0, text
    return text, path


def _func_body(asm, name):
    """Lines of function `name` (Mach-O spelling), label excluded."""
    lines = asm.splitlines()
    i = lines.index(name + ":")
    body = []
    for ln in lines[i + 1:]:
        if re.match(r"^[A-Za-z_][\w.]*:$", ln) and not ln.startswith("L"):
            break
        body.append(ln.strip())
    return body


class MachODialectTests(unittest.TestCase):
    """Tier 1: the assembler text is Mach-O, not ELF."""

    def test_symbols_are_underscore_prefixed(self):
        asm, _ = _macos_asm(HELLO)
        self.assertIn(".global _main", asm)
        self.assertIn("\n_main:", asm)
        self.assertIn("bl\t_printf", asm)
        self.assertNotIn("\nmain:", asm)

    def test_no_elf_directives(self):
        asm, _ = _macos_asm(STRESS)
        self.assertNotIn(".note.GNU-stack", asm)
        self.assertNotIn(":lo12:", asm)
        self.assertNotIn("\t.local ", asm)
        self.assertNotIn(".section .text", asm)
        self.assertIn("__TEXT,__text", asm)
        self.assertIn("__DATA,__data", asm)
        self.assertIn(".build_version macos, 11, 0", asm)

    def test_compiler_labels_are_assembler_local(self):
        # `L` labels never reach the Mach-O symbol table.
        asm, _ = _macos_asm(STRESS)
        self.assertIn("L__shivyc_label", asm)
        self.assertIsNone(re.search(r"^__shivyc_label\d+:", asm, re.M))
        self.assertIsNone(re.search(r"[^L]__shivyc_label\d+\b",
                                    asm.replace("L__shivyc_label", "")))

    def test_local_data_uses_page_relocations(self):
        asm, _ = _macos_asm(HELLO)
        self.assertRegex(asm, r"adrp\tx\d+, ___arm64str0@PAGE\n")
        self.assertRegex(asm, r"add\tx\d+, x\d+, ___arm64str0@PAGEOFF\n")

    def test_external_references_go_through_got(self):
        # Data or code that may live in libSystem cannot be reached
        # PC-relatively; ld64 relaxes the GOT load if it proves local.
        asm, _ = _macos_asm(STRESS)
        for sym in ("_environ", "_strcmp"):
            self.assertIn("%s@GOTPAGE" % sym, asm)
            self.assertIn("%s@GOTPAGEOFF]" % sym, asm)
        # A function defined here is addressed directly.
        self.assertIn("_cmp@PAGE", asm)
        self.assertNotIn("_cmp@GOTPAGE", asm)

    def test_commons_and_alignment(self):
        asm, _ = _macos_asm(STRESS)
        self.assertIn(".comm _counter,4,2", asm)
        self.assertRegex(asm, r"\.lcomm _hidden\.\d+,4,2")
        # A pointer in __data must be 8-aligned for chained fixups.
        self.assertRegex(asm, r"\.p2align 3\n_greeting:")

    def test_two_byte_data_is_short(self):
        # `.word` is 4 bytes to the arm64 assembler.
        asm, _ = _macos_asm(STRESS)
        self.assertRegex(asm, r"_shorts\.\d+:\n\t\.short 1\n\t\.short -2")
        self.assertNotIn(".word", asm)


class AppleVariadicABITests(unittest.TestCase):
    """Tier 1: Apple's arm64 variadic convention. Named arguments in
    registers; anonymous arguments on the stack in 8-byte slots from [sp].
    libSystem's printf reads them only from there."""

    def test_caller_puts_only_anonymous_args_at_sp(self):
        src = PROTOS + ('int main(void){ int a = 5; long b = 6;'
                        ' printf("%d %ld\\n", a, b); return 0; }\n')
        body = _func_body(_macos_asm(src)[0], "_main")
        call = body.index("bl\t_printf")
        before = body[:call]
        # Two anonymous args -> 16 bytes reserved, at [sp,#0] and [sp,#8].
        self.assertIn("sub\tsp, sp, #16", before)
        stores = [ln for ln in before if re.match(r"str\t\S+, \[sp, #\d+\]", ln)]
        self.assertEqual([re.search(r"#(\d+)", s).group(1) for s in stores],
                         ["0", "8"])
        # The caller no longer hands a block base over in x16.
        self.assertNotIn("mov\tx16, sp", before)

    def test_no_anonymous_args_reserves_nothing(self):
        body = _func_body(_macos_asm(HELLO)[0], "_main")
        call = body.index("bl\t_printf")
        self.assertFalse([ln for ln in body[:call] if "sub\tsp, sp" in ln])

    def test_callee_captures_entry_sp_before_prologue(self):
        # The variadic block starts at the caller's sp, i.e. ours on entry.
        # Capturing it first makes a Crust variadic function callable from
        # clang-built C, which never sets x16.
        body = _func_body(_macos_asm(STRESS)[0], "_sum")
        self.assertEqual(body[0], "mov\tx16, sp")

    def test_callee_reads_from_base_without_named_skip(self):
        body = _func_body(_macos_asm(STRESS)[0], "_sum")
        # va_start: base + 0 (ELF would skip 8 * named_count).
        self.assertTrue(any(re.match(r"add\tx\d+, x\d+, #0$", ln)
                            for ln in body), body)


class BundledHeaderTests(unittest.TestCase):
    """Tier 1: the bundled headers under --os macos. They are what
    `#include <stdio.h>` resolves to, so they must use libSystem's names."""

    def test_std_streams_use_libsystem_symbols(self):
        # libSystem exports __stdinp/__stdoutp/__stderrp; there is no
        # `_stderr`. As external data they are reached through the GOT.
        asm, _ = _macos_asm('#include <stdio.h>\n'
                            'int main(void){ fputs("x", stderr);'
                            ' fputs("y", stdout); return 0; }\n')
        self.assertIn("___stderrp@GOTPAGE", asm)
        self.assertIn("___stdoutp@GOTPAGE", asm)
        self.assertNotRegex(asm, r"\b_stderr\b|\b_stdout\b")

    def test_elf_keeps_plain_stream_names(self):
        rc, asm, _ = _compile('#include <stdio.h>\n'
                              'int main(void){ fputs("x", stderr); return 0; }\n',
                              ["-S", "--target", "arm64", "--os", "linux"])
        self.assertEqual(rc, 0, asm)
        self.assertIn("stderr", asm)
        self.assertNotIn("__stderrp", asm)

    def test_classification_macros_need_no_library(self):
        # Apple's isnan & co. are header-only; there is no `_isnan` to bind.
        asm, _ = _macos_asm(FPCLASS)
        self.assertNotRegex(asm, r"bl\t_(isnan|isinf|isfinite|signbit)\b")
        self.assertIn("bl\t___crust_isinf", asm)

    def test_long_double_is_double(self):
        # Apple arm64 defines long double as double: 8 bytes, d registers,
        # accepted silently (ELF targets still reject it).
        asm, _ = _macos_asm("long double half(long double x){ return x / 2; }\n"
                            "int main(void){ return sizeof(long double); }\n")
        self.assertIn("fdiv\td", asm)
        self.assertRegex(_func_body(asm, "_main")[0], r"mov\tw0, #8$")
        rc, text, _ = _compile("int main(void){ long double x = 1; "
                               "return (int)x; }\n",
                               ["-S", "--target", "arm64", "--os", "linux"])
        self.assertNotEqual(rc, 0)


def _qemu_arm64():
    """(cross gcc, qemu) for running AArch64 Linux code, or None."""
    gcc = shutil.which("aarch64-linux-gnu-gcc")
    qemu = shutil.which("qemu-aarch64")
    return (gcc, qemu) if gcc and qemu else None


@unittest.skipUnless(_qemu_arm64(), "needs aarch64-linux-gnu-gcc and "
                     "qemu-aarch64 (apt install gcc-aarch64-linux-gnu "
                     "qemu-user)")
class BundledHeaderExecutionTests(unittest.TestCase):
    """The macOS branch of the bundled <math.h>, *executed*. The helpers
    are plain arithmetic on the same arm64 code generator, so an AArch64
    Linux build with __APPLE__ forced on runs exactly that code under qemu.
    (stdio is left out: its __APPLE__ branch names libSystem symbols.)"""

    def test_stdarg_apple_branch(self):
        # The __APPLE__ <stdarg.h> spells va_list as the SDK does (void *)
        # and steps through char *. Mixed types and va_copy must still work.
        gcc, qemu = _qemu_arm64()
        src = '''
#include <stdarg.h>
static long mix(int n, ...) {
    va_list ap, cp; long t = 0; int i;
    va_start(ap, n);
    va_copy(cp, ap);
    for (i = 0; i < n; i++) {
        if (i % 3 == 0) t += va_arg(ap, int);
        else if (i % 3 == 1) t += va_arg(ap, long);
        else t += (long)(va_arg(ap, double) * 10);
    }
    t += va_arg(cp, int) * 1000;
    va_end(cp); va_end(ap);
    return t;
}
int main(void) { return mix(5, 1, 20L, 2.5, 4, 50L) == 1100 ? 0 : 1; }
'''
        rc, text, spath = _compile(src, ["-S", "--target", "arm64",
                                         "--os", "linux", "-D__APPLE__"])
        self.assertEqual(rc, 0, text)
        exe = spath[:-2]
        subprocess.run([gcc, spath, "-o", exe], check=True)
        env = dict(os.environ)
        env.setdefault("QEMU_LD_PREFIX", "/usr/aarch64-linux-gnu")
        self.assertEqual(subprocess.run([qemu, exe], env=env,
                                        timeout=60).returncode, 0)

    def test_classification_helpers(self):
        gcc, qemu = _qemu_arm64()
        src = (FPCLASS.replace("#include <stdio.h>", "")
               .replace('fprintf(stderr, "r=%d\\n", r);', ""))
        rc, text, spath = _compile(src, ["-S", "--target", "arm64",
                                         "--os", "linux", "-D__APPLE__"])
        self.assertEqual(rc, 0, text)
        self.assertIn("__crust_isnan", text)
        exe = spath[:-2]
        subprocess.run([gcc, spath, "-o", exe], check=True)
        env = dict(os.environ)
        env.setdefault("QEMU_LD_PREFIX", "/usr/aarch64-linux-gnu")
        p = subprocess.run([qemu, exe], env=env, timeout=60)
        self.assertEqual(p.returncode, 0)


# Headers from the SDK itself (via -I) that are known to preprocess and
# parse. Extend as more are fixed; see MACOS.md.
SDK_HEADERS_OK = [
    "stdio.h", "stdlib.h", "string.h", "strings.h", "ctype.h", "errno.h",
    "unistd.h", "limits.h", "stdint.h", "inttypes.h", "fcntl.h", "time.h",
    "signal.h", "sys/types.h", "sys/stat.h", "sys/time.h", "sys/wait.h",
    "dirent.h", "setjmp.h", "locale.h", "assert.h", "termios.h",
    "sys/mman.h", "wchar.h", "sys/socket.h", "sched.h", "pthread.h",
    "netinet/in.h", "sys/uio.h"]


@unittest.skipUnless(os.environ.get("CRUST_MACOS_SDK"),
                     "set CRUST_MACOS_SDK to an SDK's usr/include: on a Mac "
                     "$(xcrun --show-sdk-path)/usr/include, elsewhere the "
                     "stand-in from tools/macos_proxy_sdk.sh")
class SDKHeaderTests(unittest.TestCase):
    """Apple's own headers, which are written for clang: they must
    preprocess and parse, and what they declare must compile correctly."""

    def _sdk_compile(self, src):
        return _compile(src, ["-S", "--target", "arm64", "--os", "macos",
                              "-I", os.environ["CRUST_MACOS_SDK"]])

    def test_known_good_headers_together(self):
        # All in one translation unit: real programs include many, and
        # typedef/macro interactions only show up together. The sizes are
        # laid out from Apple's own definitions: struct stat is the 144-byte
        # 64-bit-inode layout on arm64, pthread_t a pointer.
        src = "".join("#include <%s>\n" % h for h in SDK_HEADERS_OK)
        src += ("int main(void){ return (int)(sizeof(struct stat)"
                " + sizeof(pthread_t)); }\n")
        rc, asm, _ = self._sdk_compile(src)
        self.assertEqual(rc, 0, asm)
        self.assertRegex(_func_body(asm, "_main")[0], r"mov\tw0, #152$")

    def test_sdk_declarations_generate_correct_calls(self):
        # printf's SDK prototype carries __restrict and __printflike; the
        # call must still be a plain Apple variadic call, and stderr must be
        # the SDK's __stderrp.
        rc, asm, _ = self._sdk_compile(
            "#include <stdio.h>\n#include <unistd.h>\n"
            "int main(void){ fprintf(stderr, \"%d\\n\", (int)getpid());"
            " return 0; }\n")
        self.assertEqual(rc, 0, asm)
        self.assertIn("___stderrp@GOTPAGE", asm)
        self.assertIn("bl\t_getpid", asm)
        body = _func_body(asm, "_main")
        call = body.index("bl\t_fprintf")
        self.assertTrue(any(re.match(r"str\t\S+, \[sp, #0\]", ln)
                            for ln in body[:call]), body)


class OSSelectionTests(unittest.TestCase):
    """Tier 1: --os plumbing and diagnostics."""

    def test_x86_64_macos_is_rejected(self):
        rc, text, _ = _compile(HELLO, ["-S", "--target", "x86_64",
                                       "--os", "macos"])
        self.assertNotEqual(rc, 0)
        self.assertIn("Apple Silicon", text)

    def test_unknown_os_is_rejected(self):
        rc, text, _ = _compile(HELLO, ["-S", "--target", "arm64",
                                       "--os", "plan9"])
        self.assertNotEqual(rc, 0)
        self.assertIn("unrecognized --os", text)

    def test_musl_is_rejected_on_macos(self):
        rc, text, _ = _compile(HELLO, ["-S", "--target", "arm64",
                                       "--os", "macos", "--musl"])
        self.assertNotEqual(rc, 0)
        self.assertIn("--musl cannot target macOS", text)

    def test_elf_output_is_unaffected(self):
        # none and linux are the same ELF output; neither has Mach-O syntax.
        outs = []
        for osn in ("linux", "none"):
            rc, text, _ = _compile(STRESS, ["-S", "--target", "arm64",
                                            "--os", osn])
            self.assertEqual(rc, 0, text)
            self.assertNotIn("@PAGE", text)
            self.assertIn(":lo12:", text)
            self.assertIn("\nmain:", text)
            outs.append(text)
        self.assertEqual(outs[0], outs[1])

    def test_resolve_os_follows_host_only_for_native_arch(self):
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import shivyc.main as m
        real = m.host_os
        try:
            m.host_os = lambda: "macos"
            self.assertEqual(m.resolve_os("arm64", ""), "macos")
            self.assertEqual(m.resolve_os("riscv64", ""), "linux")
            self.assertEqual(m.resolve_os("arm64", "none"), "none")
            self.assertEqual(m.resolve_os("arm64", "darwin"), "macos")
            m.host_os = lambda: "linux"
            self.assertEqual(m.resolve_os("arm64", ""), "linux")
        finally:
            m.host_os = real


def _llvm_macho_tools():
    """(clang, ld64.lld, llvm-nm) if all present, else None."""
    clang = shutil.which("clang")
    nm = shutil.which("llvm-nm")
    lld = shutil.which("ld64.lld")
    if lld is None:
        for v in range(30, 13, -1):
            lld = shutil.which("ld64.lld-%d" % v)
            if lld:
                break
    if clang and lld and nm:
        return clang, lld, nm
    return None


@unittest.skipUnless(_llvm_macho_tools(),
                     "needs clang, ld64.lld and llvm-nm (apt install clang "
                     "lld llvm)")
class MachOAssembleAndLinkTests(unittest.TestCase):
    """Tier 2: the output is accepted by a real Mach-O assembler and linker."""

    def _link(self, src):
        clang, lld, nm = _llvm_macho_tools()
        asm, spath = _macos_asm(src)
        d = os.path.dirname(spath)
        obj = os.path.join(d, "t.o")
        subprocess.run([clang, "-target", "arm64-apple-macos11", "-c", spath,
                        "-o", obj], check=True, capture_output=True)
        undef = subprocess.run([nm, "-u", obj], capture_output=True,
                               text=True, check=True).stdout.split()
        tbd = os.path.join(d, "libSystem.tbd")
        with open(tbd, "w") as f:
            f.write("--- !tapi-tbd\ntbd-version: 4\n"
                    "targets: [ arm64-macos ]\n"
                    "install-name: '/usr/lib/libSystem.B.dylib'\n"
                    "exports:\n  - targets: [ arm64-macos ]\n"
                    "    symbols: [ %s ]\n...\n"
                    % ", ".join(undef + ["dyld_stub_binder"]))
        exe = os.path.join(d, "t")
        p = subprocess.run([lld, "-arch", "arm64", "-platform_version",
                            "macos", "11.0", "11.0", "-o", exe, obj, tbd],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return obj, exe

    def test_hello_links_to_pie_executable(self):
        _, exe = self._link(HELLO)
        with open(exe, "rb") as f:
            hdr = f.read(32)
        self.assertEqual(hdr[:4], b"\xcf\xfa\xed\xfe")      # MH_MAGIC_64
        self.assertEqual(hdr[4:8], b"\x0c\x00\x00\x01")     # CPU_TYPE_ARM64
        self.assertEqual(hdr[12:16], b"\x02\x00\x00\x00")   # MH_EXECUTE
        flags = int.from_bytes(hdr[24:28], "little")
        self.assertTrue(flags & 0x200000, "not PIE")        # MH_PIE

    def test_stress_links_and_imports_only_libc(self):
        _, _, nm = _llvm_macho_tools()
        obj, _ = self._link(STRESS)
        undef = subprocess.run([nm, "-u", obj], capture_output=True,
                               text=True).stdout.split()
        self.assertEqual(sorted(undef),
                         ["_environ", "_printf", "_qsort", "_strcmp"])


def _have_unicorn():
    try:
        import unicorn  # noqa: F401
        return True
    except ImportError:
        return False


ABI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "macos_abi")
ABI_CHECK_NAMES = [
    "many: named stack args packed at natural size",
    "manyd: 9th+ FP args", "take_sc: signed char arg",
    "take_us: unsigned short arg", "take_b: _Bool arg",
    "ret_sc: signed char return", "ret_us: unsigned short return (callee extends)",
    "vsum: variadic", "vsum(0)", None, None, None,
    "twof: two stack floats in 4-byte slots",
    "gap: alignment padding between stack args",
    "id_sc: runtime signed char arg (caller extends)",
    "id_uc: runtime unsigned char arg", "id_ss: runtime short arg",
    "narrow_sc: narrowing signed char return",
    "narrow_uc: narrowing unsigned char return",
    "many via function pointer"]


@unittest.skipUnless(_llvm_macho_tools() and _have_unicorn(),
                     "needs clang, ld64.lld, llvm-nm and unicorn (apt install "
                     "clang lld llvm; pip install unicorn)")
class AppleABIExecutionTests(unittest.TestCase):
    """Apple's arm64 calling convention, *executed*: every caller/callee
    pairing of Crust and clang (the ABI's reference implementation), run by
    tools/macho_run.py under emulation. Covers the rules where Apple departs
    from AAPCS64: stack arguments packed at natural size and alignment, the
    caller extending sub-int arguments, the callee extending sub-int
    returns, and variadic arguments on the stack."""

    @classmethod
    def setUpClass(cls):
        clang, cls.lld, _ = _llvm_macho_tools()
        cls.clang = clang
        cls.dir = tempfile.mkdtemp()
        cls.objs = {}
        for who in ("cl", "cr"):
            cls.objs[("funcs", who)] = cls._obj(who, "abi_funcs.c", who)
        tbd = os.path.join(cls.dir, "libSystem.tbd")
        with open(tbd, "w") as f:
            f.write("--- !tapi-tbd\ntbd-version: 4\n"
                    "targets: [ arm64-macos ]\n"
                    "install-name: '/usr/lib/libSystem.B.dylib'\n"
                    "exports:\n  - targets: [ arm64-macos ]\n"
                    "    symbols: [ dyld_stub_binder ]\n...\n")
        cls.tbd = tbd

    @classmethod
    def _obj(cls, compiler, src, pfx, opfx=None):
        """Compile tests/macos_abi/<src> with PFX (and OPFX) defined."""
        tag = "%s_%s_%s_%s" % (os.path.splitext(src)[0], compiler, pfx,
                               opfx or "")
        obj = os.path.join(cls.dir, tag + ".o")
        defs = ["-DPFX=%s_" % pfx] + (["-DOPFX=%s_" % opfx] if opfx else [])
        path = os.path.join(ABI_DIR, src)
        if compiler == "cl":
            subprocess.run([cls.clang, "-target", "arm64-apple-macos11",
                            "-O1", "-c", path, "-o", obj] + defs,
                           check=True, capture_output=True)
            return obj
        rc, text, spath = _compile(open(path).read(),
                                   ["-S", "--target", "arm64", "--os",
                                    "macos", "-I", ABI_DIR] + defs)
        assert rc == 0, text
        subprocess.run([cls.clang, "-target", "arm64-apple-macos11", "-c",
                        spath, "-o", obj], check=True, capture_output=True)
        return obj

    def _pairing(self, caller, callee):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import macho_run
        pfx = "x%s%s" % (caller, callee)
        objs = [self._obj(caller, "abi_checks.c", pfx, callee),
                self._obj("cr", "abi_main.c", pfx),
                self.objs[("funcs", callee)]]
        exe = os.path.join(self.dir, pfx)
        p = subprocess.run([self.lld, "-arch", "arm64", "-platform_version",
                            "macos", "11.0", "11.0", "-no_fixup_chains",
                            "-o", exe] + objs + [self.tbd],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        r = macho_run.run(exe)
        bad = [n for i, n in enumerate(ABI_CHECK_NAMES) if n and (r >> i) & 1]
        self.assertEqual(bad, [], "%s calling %s" % (caller, callee))

    def test_clang_calls_clang_control(self):
        # Validates the test itself: the reference ABI against itself.
        self._pairing("cl", "cl")

    def test_crust_calls_clang(self):
        self._pairing("cr", "cl")

    def test_clang_calls_crust(self):
        self._pairing("cl", "cr")

    def test_crust_calls_crust(self):
        self._pairing("cr", "cr")


@unittest.skipUnless(platform.system() == "Darwin"
                     and shutil.which("cc") is not None,
                     "runs only on an Apple Silicon Mac")
class MacOSNativeRunTests(unittest.TestCase):
    """Tier 3: build with the system toolchain and run it."""

    def _run(self, src):
        rc, text, exe = _compile(src, ["--target", "arm64", "--os", "macos"],
                                 out_name="t")
        self.assertEqual(rc, 0, text)
        return subprocess.run([exe], capture_output=True, text=True,
                              timeout=30)

    def test_hello_runs(self):
        p = self._run(HELLO)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "hello crust\n")

    def test_stress_runs(self):
        p = self._run(STRESS)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, STRESS_OUT)

    def test_bundled_headers_run(self):
        # <stdio.h> streams and <math.h> classification, against libSystem.
        p = self._run(FPCLASS)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, "r=1023\n")


if __name__ == "__main__":
    unittest.main()
