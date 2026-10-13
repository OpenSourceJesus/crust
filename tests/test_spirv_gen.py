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
