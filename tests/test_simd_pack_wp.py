"""Whole-program promotion of global flags into xmm15 across translation units.

The single-TU `-fsimd-pack-globals` feature packs 1-8 bit `*_Nbit` *static*
flags into xmm15. Seeing the whole program lets ShivyC do this for
*externally-linked* flags too: a flag defined in one unit and read in a hot
function in another is served from the same xmm15 bit in both, using one shared
layout and one shared memory mirror. A flag whose address is taken anywhere is
excluded (a pointer write would bypass the register cache).

These drive the real multi-file CLI, run the binary, and inspect assembly.
"""

import os
import subprocess
import tempfile
import unittest


def _write(d, name, text):
    path = os.path.join(d, name)
    with open(path, "w") as f:
        f.write(text)
    return path


def _compile(files, out, extra=()):
    env = dict(os.environ, SHIVYC_CACHE_DIR=os.path.join(
        os.path.dirname(out), "cache"))
    proc = subprocess.run(["shivyc", *extra, *files, "-o", out],
                          env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def _asm_for(c_path):
    s = os.path.splitext(c_path)[0] + ".s"
    return open(s).read() if os.path.exists(s) else ""


class TestWholeProgramFlagPacking(unittest.TestCase):
    def test_cross_tu_flag_read_from_register_and_correct(self):
        d = tempfile.mkdtemp()
        defs = _write(d, "defs.c",
                      "unsigned char ready_1bit;\n"
                      "unsigned char mode_3bit;\n"
                      "int probe_hot(void);\n"
                      "int main(void){ ready_1bit=1; mode_3bit=5;"
                      " return probe_hot(); }\n")
        mod = _write(d, "mod.c",
                     "extern unsigned char ready_1bit;\n"
                     "extern unsigned char mode_3bit;\n"
                     "int probe_hot(void){ return ready_1bit + mode_3bit*10; }\n")
        out = os.path.join(d, "prog")
        rc, _ = _compile([defs, mod], out, extra=["-fsimd-pack-globals"])
        self.assertEqual(rc, 0)
        # The flags set in defs.c's main must be visible (via xmm15) in mod.c.
        self.assertEqual(subprocess.run([out]).returncode, 51)
        # The hot function in the other TU serves both flags from the register.
        mod_asm = _asm_for(mod)
        self.assertIn("zero-latency read ready_1bit", mod_asm)
        self.assertIn("zero-latency read mode_3bit", mod_asm)

    def test_mirror_is_shared_common_symbol(self):
        d = tempfile.mkdtemp()
        defs = _write(d, "defs.c",
                      "unsigned char ready_1bit;\n"
                      "int probe_hot(void);\n"
                      "int main(void){ ready_1bit=1; return probe_hot(); }\n")
        mod = _write(d, "mod.c",
                     "extern unsigned char ready_1bit;\n"
                     "int probe_hot(void){ return ready_1bit; }\n")
        rc, _ = _compile([defs, mod], os.path.join(d, "prog"),
                         extra=["-fsimd-pack-globals"])
        self.assertEqual(rc, 0)
        # Both units declare the mirror, and as a NON-local common symbol so the
        # linker merges them into a single shared object.
        for c in (defs, mod):
            asm = _asm_for(c)
            self.assertIn(".comm __simd_pack_store, 8", asm)
            self.assertNotIn(".local __simd_pack_store", asm)

    def test_address_taken_flag_excluded(self):
        d = tempfile.mkdtemp()
        defs = _write(d, "defs.c",
                      "unsigned char ready_1bit;\n"
                      "unsigned char esc_1bit;\n"
                      "unsigned char* leak(void);\n"
                      "int read_hot(void);\n"
                      "int main(void){ ready_1bit=1; esc_1bit=1;"
                      " unsigned char* p=leak(); *p=0; return read_hot(); }\n")
        mod = _write(d, "mod.c",
                     "extern unsigned char ready_1bit;\n"
                     "extern unsigned char esc_1bit;\n"
                     "unsigned char* leak(void){ return &esc_1bit; }\n"
                     "int read_hot(void){ return ready_1bit + esc_1bit; }\n")
        out = os.path.join(d, "prog")
        rc, _ = _compile([defs, mod], out, extra=["-fsimd-pack-globals"])
        self.assertEqual(rc, 0)
        mod_asm = _asm_for(mod)
        # ready_1bit is packed; esc_1bit is excluded (address escapes).
        self.assertIn("zero-latency read ready_1bit", mod_asm)
        self.assertNotIn("zero-latency read esc_1bit", mod_asm)
        # Correct at runtime: the pointer write to esc_1bit is honored.
        self.assertEqual(subprocess.run([out]).returncode, 1)

    def test_single_tu_flag_still_packed(self):
        # A single-file build keeps the original per-TU flag packing.
        d = tempfile.mkdtemp()
        src = _write(d, "s.c",
                     "unsigned char ready_1bit;\n"
                     "int probe_hot(void){ return ready_1bit; }\n"
                     "int main(void){ ready_1bit=1; return probe_hot(); }\n")
        out = os.path.join(d, "s")
        rc, _ = _compile([src], out, extra=["-fsimd-pack-globals"])
        self.assertEqual(rc, 0)
        self.assertIn("zero-latency read ready_1bit", _asm_for(src))
        self.assertEqual(subprocess.run([out]).returncode, 1)


if __name__ == "__main__":
    unittest.main()
