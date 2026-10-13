"""End-to-end tests for `--target spirv`: compile a C kernel, validate it, run
it in the reference interpreter, and compare with the same source built
natively by gcc (shivyc/include/crust_gpu.h supplies the gpu_* builtins)."""
import os, struct, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from shivyc import spirv_reader
from shivyc.spirv import bytes_to_words
import spirv_interp

GPU = os.path.join(ROOT, "tests", "gpu")


def compile_spirv(src, tmp, env=None):
    out = os.path.join(tmp, "k.spv")
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run([sys.executable, "-m", "shivyc.main", "--target",
                        "spirv", src, "-o", out], cwd=ROOT, env=e,
                       capture_output=True, text=True)
    return p, out


def modes(name, tmp, structure="1"):
    """{function: how it was lowered} as reported by the back end."""
    p, _ = compile_spirv(os.path.join(GPU, name), tmp,
                         {"CRUST_SPIRV_REPORT": "1",
                          "CRUST_SPIRV_STRUCT": structure})
    assert p.returncode == 0, p.stdout + p.stderr
    out = {}
    for line in p.stderr.splitlines():
        if line.startswith("spirv: "):
            _tag, fn, how = line.split(": ", 2)
            out[fn] = how
    return out


def build(name, tmp):
    p, out = compile_spirv(os.path.join(GPU, name), tmp)
    assert p.returncode == 0, p.stdout + p.stderr
    w = bytes_to_words(open(out, "rb").read())
    assert spirv_reader.validate(w) == []
    return w


def native(name, tmp, driver):
    """Build the kernel with gcc plus a C driver, run it, return stdout."""
    src = os.path.join(GPU, name)
    kern = open(src).read().replace("unsigned gpu_global_id(int dim);", "")
    kern = kern.replace("float sqrtf(float x);", "#include <math.h>")
    kern = kern.replace("float fabsf(float x);", "#include <math.h>")
    kern = kern.replace("unsigned gpu_local_id(int dim);", "")
    # strip the header assert lines, which are not C
    kern = "\n".join(l for l in kern.splitlines()
                     if not l.startswith("assert "))
    f = os.path.join(tmp, "n.c")
    open(f, "w").write('#include <crust_gpu.h>\n' + kern + "\n" + driver)
    exe = os.path.join(tmp, "n")
    subprocess.run(["gcc", "-O1", "-idirafter", os.path.join(ROOT, "shivyc", "include"),
                    f, "-o", exe, "-lm"], check=True)
    return subprocess.run([exe], capture_output=True, text=True,
                          check=True).stdout.split()


class TestSpirvGen(unittest.TestCase):
    def test_vadd_matches_native(self):
        with tempfile.TemporaryDirectory() as t:
            w = build("vadd.c", t)
            n = 100
            a = [float(i) for i in range(n)]
            b = [0.5 * i for i in range(n)]
            c = bytearray(4 * n)
            spirv_interp.run(w, "vadd", (2, 1, 1),
                             {0: bytearray(struct.pack("100f", *a)),
                              1: bytearray(struct.pack("100f", *b)), 2: c},
                             push=struct.pack("I", n))
            got = struct.unpack("100f", bytes(c))
            drv = ('int main(void){float a[100],b[100],c[100];int i;'
                   'for(i=0;i<100;i++){a[i]=i;b[i]=0.5f*i;c[i]=0;}'
                   'GPU_LAUNCH(2,1,1,64,1,1,vadd(a,b,c,100));'
                   'for(i=0;i<100;i++)printf("%.9g\\n",c[i]);return 0;}')
            ref = native("vadd.c", t, "#include <stdio.h>\n" + drv)
            self.assertEqual([float(x) for x in ref], list(got))

    def test_loop_and_helper_call_match_native(self):
        with tempfile.TemporaryDirectory() as t:
            w = build("loop.c", t)
            m = [float(i) for i in range(12)]
            o = bytearray(16)
            spirv_interp.run(w, "rowsum", (1, 1, 1),
                             {0: bytearray(struct.pack("12f", *m)), 1: o},
                             push=struct.pack("I", 3))
            got = list(struct.unpack("4i", bytes(o)))
            drv = ('int main(void){float m[12];int o[4],i;'
                   'for(i=0;i<12;i++)m[i]=i;'
                   'GPU_LAUNCH(1,1,1,4,1,1,rowsum(m,o,3));'
                   'for(i=0;i<4;i++)printf("%d\\n",o[i]);return 0;}')
            ref = native("loop.c", t, "#include <stdio.h>\n" + drv)
            self.assertEqual([int(x) for x in ref], got)

    def test_stress_matches_native(self):
        import random
        rnd = random.Random(7)
        n = 64
        vals = [rnd.randint(-50, 5000) for _ in range(n)]
        with tempfile.TemporaryDirectory() as t:
            w = build("stress.c", t)
            out = bytearray(4 * n)
            fl = bytearray(4 * n)
            spirv_interp.run(w, "stress", (2, 2, 1),
                             {0: bytearray(struct.pack("64i", *vals)),
                              1: out, 2: fl}, push=struct.pack("I", n))
            drv = ("#include <stdio.h>\nint main(void){int in[64]={%s},o[64];"
                   "float f[64];int i;GPU_LAUNCH(2,2,1,8,2,1,"
                   "stress(in,o,f,64));for(i=0;i<64;i++)"
                   'printf("%%d %%.9g\\n",o[i],f[i]);return 0;}'
                   % ",".join(map(str, vals)))
            ref = native("stress.c", t, drv).copy()
            ref_o = [int(x) for x in ref[0::2]]
            ref_f = [struct.unpack("f", struct.pack("f", float(x)))[0]
                     for x in ref[1::2]]
            self.assertEqual(ref_o, list(struct.unpack("64i", bytes(out))))
            self.assertEqual(ref_f, list(struct.unpack("64f", bytes(fl))))

    def test_control_flow_is_structured_not_dispatched(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(modes("vadd.c", t), {"vadd": "structured"})
            self.assertEqual(modes("loop.c", t), {"rowsum": "structured",
                                                  "clampi": "structured"})
            self.assertEqual(modes("stress.c", t),
                             {"stress": "structured",
                              "collatz": "structured"})

    def test_dispatcher_fallback_agrees_with_structured(self):
        with tempfile.TemporaryDirectory() as t:
            vals = [(-7 * i) % 1000 + i for i in range(64)]
            results = []
            for structure in ("1", "0"):
                p, out = compile_spirv(os.path.join(GPU, "stress.c"), t,
                                       {"CRUST_SPIRV_STRUCT": structure})
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                w = bytes_to_words(open(out, "rb").read())
                o = bytearray(256)
                f = bytearray(256)
                spirv_interp.run(w, "stress", (2, 2, 1),
                                 {0: bytearray(struct.pack("64i", *vals)),
                                  1: o, 2: f}, push=struct.pack("I", 64))
                results.append((bytes(o), bytes(f)))
            self.assertEqual(results[0], results[1])
            self.assertEqual(modes("stress.c", t, "0")["stress"],
                             "dispatcher: disabled")

    def test_two_kernels_in_one_module(self):
        with tempfile.TemporaryDirectory() as t:
            w = build("two.c", t)
            data = [3, 9, 27, 81]
            for name, want in (("inc", [4, 10, 28, 82]),
                               ("dbl", [6, 18, 54, 162])):
                o = bytearray(16)
                spirv_interp.run(w, name, (1, 1, 1),
                                 {0: bytearray(struct.pack("4I", *data)),
                                  1: o})
                self.assertEqual(list(struct.unpack("4I", bytes(o))), want)

    def test_fuzz_smoke(self):
        """A few random kernels through structured, dispatcher and gcc. The
        long run is `make fuzz_spirv`."""
        import spirv_fuzz
        errs, stats, src = spirv_fuzz.run_batch(7, 10)
        self.assertEqual(errs, [], "\n".join(errs) + "\n" + src)
        self.assertEqual(stats["dispatcher"], 0)

    def _rust_kernel(self, t):
        p, out = compile_spirv(os.path.join(GPU, "rkernel.rs"), t)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        w = bytes_to_words(open(out, "rb").read())
        self.assertEqual(spirv_reader.validate(w), [])
        return w

    def test_kernel_float_literals_are_single(self):
        src = ("#[kernel(local_size = 8)]\n"
               "fn k(a: &mut [f32]) { let i: u32 = gpu_global_id(0);\n"
               "    a[i as usize] = 1.0 / a[i as usize]; }\n")
        with tempfile.TemporaryDirectory() as t:
            f = os.path.join(t, "lit.rs")
            open(f, "w").write(src)
            out = os.path.join(t, "lit.spv")
            r = subprocess.run([sys.executable, "-m", "shivyc.main",
                                "--target", "spirv", f, "-o", out],
                               cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    # ---- workgroup memory and barriers (SPIRV.md phase 6)

    def test_reduce_with_shared_memory(self):
        with tempfile.TemporaryDirectory() as t:
            w = build("reduce.c", t)
        for n in (1, 64, 100, 300):
            data = [(i * 7 + 3) % 1000 for i in range(n)]
            groups = (n + 63) // 64
            inb = bytearray(struct.pack("<%dI" % n, *data))
            out = bytearray(4 * groups)
            spirv_interp.run(w, "reduce", (groups, 1, 1),
                             {0: inb, 1: out}, struct.pack("<I", n))
            got = struct.unpack("<%dI" % groups, bytes(out))
            want = [sum(data[g * 64:(g + 1) * 64]) for g in range(groups)]
            self.assertEqual(list(got), want, n)

    def test_rust_reduce_with_workgroup_static(self):
        with tempfile.TemporaryDirectory() as t:
            w = build("rreduce.rs", t)
        n = 130
        data = [(i * 13 + 1) % 500 for i in range(n)]
        inb = bytearray(struct.pack("<%dI" % n, *data))
        out = bytearray(12)
        spirv_interp.run(w, "reduce", (3, 1, 1), {0: inb, 1: out},
                         struct.pack("<I", n))
        want = [sum(data[g * 64:(g + 1) * 64]) for g in range(3)]
        self.assertEqual(list(struct.unpack("<3I", bytes(out))), want)

    def test_atomics_and_popcount(self):
        with tempfile.TemporaryDirectory() as t:
            w = build("hist.c", t)
        n = 75
        keys = [(i * 2654435761) & 0xFFFF for i in range(n)]
        groups = (n + 31) // 32
        glob = bytearray(64)
        pop = bytearray(4 * n)
        mx = bytearray(8)
        spirv_interp.run(w, "hist", (groups, 1, 1),
                         {0: bytearray(struct.pack("<%dI" % n, *keys)),
                          1: glob, 2: pop, 3: mx}, struct.pack("<I", n))
        want = [0] * 16
        for k in keys:
            want[k & 15] += 1
        self.assertEqual(list(struct.unpack("<16I", bytes(glob))), want)
        self.assertEqual(list(struct.unpack("<%dI" % n, bytes(pop))),
                         [bin(k).count("1") for k in keys])
        got_max, slot = struct.unpack("<2I", bytes(mx))
        self.assertEqual(got_max, max(keys))
        # the first workgroup to run wins the cas: 0 -> (or of its keys) + 1
        first = 0
        for k in keys[:32]:
            first |= k
        self.assertEqual(slot, first + 1)

    def test_atomic_needs_a_pointer_into_a_buffer(self):
        msg = self._refused("unsigned l = gpu_local_id(0);"
                            " o[l] = gpu_atomic_add(l, 1);",
                            "unsigned gpu_atomic_add(unsigned p, unsigned v);")
        self.assertIn("gpu_atomic_add", msg)

    def test_rust_atomics_and_count_ones(self):
        with tempfile.TemporaryDirectory() as t:
            w = build("rhist.rs", t)
        n = 70
        keys = [(i * 40503 + 7) & 0xFFFF for i in range(n)]
        glob = bytearray(64)
        spirv_interp.run(w, "hist", (3, 1, 1),
                         {0: bytearray(struct.pack("<%dI" % n, *keys)),
                          1: glob}, struct.pack("<I", n))
        want = [0] * 16
        for k in keys:
            want[k & 15] += bin(k).count("1") & 1
        self.assertEqual(list(struct.unpack("<16I", bytes(glob))), want)

    def _refused(self, body, extra=""):
        src = ("unsigned gpu_global_id(int d); unsigned gpu_local_id(int d);"
               " void gpu_barrier(void);\n"
               "static unsigned sh[64]; static unsigned big[8192];\n"
               + extra +
               "void k(unsigned *o, unsigned n)\n"
               "assert k in gpu.compute(local_size=64)\n"
               "assert o in gpu.buffer(binding=0)\n"
               "assert sh in gpu.shared\n{\n" + body + "\n}\n")
        with tempfile.TemporaryDirectory() as t:
            f = os.path.join(t, "r.c")
            open(f, "w").write(src)
            p, _ = compile_spirv(f, t)
        self.assertNotEqual(p.returncode, 0, "expected a refusal")
        return p.stdout + p.stderr

    def test_barrier_under_thread_id_test_refused(self):
        msg = self._refused("unsigned l = gpu_local_id(0);"
                            " if (l < 8) { sh[l] = 1; gpu_barrier(); }")
        self.assertIn("gpu_barrier", msg)
        self.assertIn("differ between invocations", msg)

    def test_barrier_after_divergent_early_return_refused(self):
        msg = self._refused("unsigned g = gpu_global_id(0);"
                            " if (g >= n) return; sh[gpu_local_id(0)] = g;"
                            " gpu_barrier(); o[g] = sh[0];")
        self.assertIn("differ between invocations", msg)

    def test_barrier_in_uniform_branch_accepted(self):
        with tempfile.TemporaryDirectory() as t:
            f = os.path.join(t, "u.c")
            open(f, "w").write(
                "unsigned gpu_local_id(int d); void gpu_barrier(void);\n"
                "static unsigned sh[64];\n"
                "void k(unsigned *o, unsigned n)\n"
                "assert k in gpu.compute(local_size=64)\n"
                "assert o in gpu.buffer(binding=0)\n"
                "assert sh in gpu.shared\n"
                "{ sh[gpu_local_id(0)] = 1; if (n > 3) gpu_barrier();"
                " o[gpu_local_id(0)] = sh[0]; }\n")
            p, _ = compile_spirv(f, t)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_undeclared_static_still_refused(self):
        msg = self._refused("unsigned l = gpu_local_id(0);"
                            " extra_state = l; o[l] = extra_state;",
                            "static unsigned extra_state;")
        self.assertIn("gpu.shared", msg)

    def test_shared_memory_over_limit_refused(self):
        with tempfile.TemporaryDirectory() as t:
            f = os.path.join(t, "big.c")
            open(f, "w").write(
                "unsigned gpu_local_id(int d);\nstatic unsigned big[8192];\n"
                "void k(unsigned *o)\n"
                "assert k in gpu.compute(local_size=64)\n"
                "assert o in gpu.buffer(binding=0)\n"
                "assert big in gpu.shared\n"
                "{ big[gpu_local_id(0)] = 1; o[0] = big[1]; }\n")
            p, _ = compile_spirv(f, t)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("16384", p.stdout + p.stderr)

    def test_barrier_in_helper_follows_its_call_site(self):
        helper = "void sync(void) { gpu_barrier(); }\n"
        msg = self._refused("unsigned l = gpu_local_id(0);"
                            " if (l < 8) sync(); o[0] = sh[0];", helper)
        self.assertIn("differ between invocations", msg)
        with tempfile.TemporaryDirectory() as t:
            f = os.path.join(t, "h.c")
            open(f, "w").write(
                "unsigned gpu_local_id(int d); void gpu_barrier(void);\n"
                "static unsigned sh[64];\n" + helper +
                "void k(unsigned *o)\n"
                "assert k in gpu.compute(local_size=64)\n"
                "assert o in gpu.buffer(binding=0)\n"
                "assert sh in gpu.shared\n"
                "{ sh[gpu_local_id(0)] = 2; sync(); o[gpu_local_id(0)]"
                " = sh[(gpu_local_id(0) + 1) % 64]; }\n")
            p, out = compile_spirv(f, t)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            w = bytes_to_words(open(out, "rb").read())
        buf = bytearray(256)
        spirv_interp.run(w, "k", (1, 1, 1), {0: buf})
        self.assertEqual(set(struct.unpack("<64I", bytes(buf))), {2})

    def test_rust_kernel_interface(self):
        with tempfile.TemporaryDirectory() as t:
            iface = spirv_reader.describe(self._rust_kernel(t))
        self.assertEqual(len(iface), 1)
        k = iface[0]
        self.assertEqual(k["name"], "shape")
        self.assertEqual(k["local_size"], [8, 1, 1])
        # #[binding(N)] pins the numbers; readonly follows & vs &mut
        self.assertEqual([(b["binding"], b["name"], b["access"])
                          for b in k["buffers"]],
                         [(2, "xs", "read"), (5, "out", "read_write")])
        self.assertEqual([(m["name"], m["offset"], m["type"])
                          for m in k["push"]],
                         [("bias", 0, "f32"), ("n", 4, "u32")])

    def test_rust_kernel_math_consts_and_bool(self):
        import math
        with tempfile.TemporaryDirectory() as t:
            w = self._rust_kernel(t)
            xs = [-2.5, -0.75, -0.1, 0.0, 0.5, 0.99, 1.0, 3.25, 9.0]
            n = len(xs)
            bias = 1.5
            out = bytearray(4 * n)
            spirv_interp.run(w, "shape", (2, 1, 1),
                             {2: bytearray(struct.pack("%df" % n, *xs)),
                              5: out}, push=struct.pack("fI", bias, n))
            got = struct.unpack("%df" % n, bytes(out))
            f32 = lambda v: struct.unpack("f", struct.pack("f", v))[0]
            for i, x in enumerate(xs):
                acc = 0.0
                for k in range(3):
                    acc = f32(acc + f32(f32(x * 2.5) + k))
                if -1.0 <= x <= 1.0:
                    acc = max(acc, bias)
                acc = min(max(acc, -10.0), 10.0)
                want = f32(math.sqrt(abs(acc))) + f32(x - math.floor(x))
                self.assertAlmostEqual(got[i], want, places=4, msg=str(x))

    def test_rust_kernel_attribute_errors(self):
        cases = {
            "no local_size": "#[kernel]\nfn k(a: &[f32]) { }\n",
            "kernel returns": "#[kernel(local_size = 8)]\nfn k(a: &[f32]) "
                              "-> u32 { 0 }\n",
            "bad param attribute": "#[kernel(local_size = 8)]\n"
                                   "fn k(#[oops] a: &[f32]) { }\n",
        }
        for name, src in cases.items():
            with tempfile.TemporaryDirectory() as t:
                f = os.path.join(t, "bad.rs")
                open(f, "w").write(src)
                p, _ = compile_spirv(f, t)
                self.assertNotEqual(p.returncode, 0, name)

    def test_refuses_unsupported(self):
        cases = {
            "recursion": "int f(int x){return x?f(x-1):0;}\n"
                         "void k(int *o) assert k in gpu.compute(local_size=1)\n"
                         "assert o in gpu.buffer(binding=0)\n{ o[0]=f(3); }\n",
            "libc": "int puts(const char *s);\n"
                    "void k(int *o) assert k in gpu.compute(local_size=1)\n"
                    "assert o in gpu.buffer(binding=0)\n{ puts(\"x\"); }\n",
        }
        for name, src in cases.items():
            with tempfile.TemporaryDirectory() as t:
                f = os.path.join(t, name + ".c")
                open(f, "w").write(src)
                p, _ = compile_spirv(f, t)
                self.assertNotEqual(p.returncode, 0, name)
                self.assertIn("spirv", (p.stdout + p.stderr).lower(), name)


if __name__ == "__main__":
    unittest.main()
