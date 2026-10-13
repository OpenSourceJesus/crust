#!/usr/bin/env python3
"""Differential fuzzer for the SPIR-V back end's control flow.

Generates random kernels (if/else, bounded loops, break, continue, early
return, && || !), compiles a batch with `--target spirv` twice -- once with the
structurizer, once forced onto the dispatcher -- runs both in the reference
interpreter, and compares every output with the same source built natively by
gcc through shivyc/include/crust_gpu.h.

    python3 tools/spirv_fuzz.py [--seed N] [--kernels N] [--batch N]

Everything is unsigned so gcc and SPIR-V agree without undefined behaviour.
Exit status is non-zero on any mismatch; the offending source is printed.
"""
import argparse
import os
import random
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

VARS = ["a", "b", "c", "d"]
NIN = 8          # invocations per kernel
LOOP_CAP = 6


class Gen:
    def __init__(self, rnd):
        self.r = rnd
        self.loop_depth = 0
        self.loop_ids = 0
        self.budget = 0

    def const(self):
        return str(self.r.choice([0, 1, 2, 3, 5, 7, 13, 255]))

    def expr(self, depth=0):
        r = self.r
        if depth > 2 or r.random() < 0.35:
            return r.choice(VARS) if r.random() < 0.7 else self.const() + "u"
        op = r.choice(["+", "-", "*", "&", "|", "^", ">>", "<<"])
        x = self.expr(depth + 1)
        y = self.expr(depth + 1)
        if op in (">>", "<<"):
            return "(%s %s (%s & 7u))" % (x, op, y)
        return "(%s %s %s)" % (x, op, y)

    def cmp(self):
        op = self.r.choice(["<", ">", "==", "!=", "<=", ">="])
        return "%s %s %s" % (self.expr(1), op, self.expr(1))

    def cond(self, depth=0):
        r = self.r
        x = r.random()
        if depth < 2 and x < 0.3:
            return "(%s) %s (%s)" % (self.cond(depth + 1),
                                     r.choice(["&&", "||"]),
                                     self.cond(depth + 1))
        if depth < 2 and x < 0.4:
            return "!(%s)" % self.cond(depth + 1)
        return self.cmp()

    def block(self, depth, ind):
        n = self.r.randint(1, 3)
        out = []
        for _ in range(n):
            out.append(self.stmt(depth, ind))
        return "".join(out)

    def stmt(self, depth, ind):
        r = self.r
        pad = "    " * ind
        self.budget -= 1
        x = r.random()
        if self.budget <= 0 or depth >= 3 or x < 0.30:
            return "%s%s = %s;\n" % (pad, r.choice(VARS), self.expr())
        if x < 0.55:
            s = "%sif (%s) {\n%s%s}" % (pad, self.cond(), self.block(
                depth + 1, ind + 1), pad)
            if r.random() < 0.5:
                s += " else {\n%s%s}" % (self.block(depth + 1, ind + 1), pad)
            return s + "\n"
        if x < 0.72:
            self.loop_ids += 1
            t = "t%d" % self.loop_ids
            self.loop_depth += 1
            body = self.block(depth + 1, ind + 1)
            self.loop_depth -= 1
            if r.random() < 0.5:
                return ("%sfor (%s = 0; %s < %du && (%s); %s++) {\n%s%s}\n"
                        % (pad, t, t, r.randint(1, LOOP_CAP), self.cond(), t,
                           body, pad))
            return ("%s%s = 0;\n%swhile (%s < %du) {\n%s++;\n%s%s}\n"
                    % (pad, t, pad, t, r.randint(1, LOOP_CAP),
                       "    " * (ind + 1) + t, body, pad))
        if x < 0.84 and self.loop_depth:
            kw = r.choice(["break", "continue"])
            return "%sif (%s) %s;\n" % (pad, self.cond(), kw)
        if x < 0.92:
            return "%sif (%s) { out[i] = %s; return; }\n" % (
                pad, self.cond(), self.expr())
        return "%s%s = (%s) ? %s : %s;\n" % (
            pad, r.choice(VARS), self.cond(), self.expr(1), self.expr(1))

    def kernel(self, name):
        self.loop_ids = 0
        self.budget = self.r.randint(4, 14)
        # loop counters are declared up front so every path sees them
        body = self.block(0, 1)
        decl = "".join("    unsigned t%d = 0;\n" % (k + 1)
                       for k in range(self.loop_ids))
        return (
            "void %s(unsigned *in, unsigned *out)\n"
            "assert %s in gpu.compute(local_size=%d)\n"
            "assert in in gpu.buffer(binding=0, readonly)\n"
            "assert out in gpu.buffer(binding=1)\n"
            "{\n"
            "    unsigned i = gpu_global_id(0);\n"
            "    unsigned a = in[i], b = in[i + %d], c = in[i] * 3u + 1u, "
            "d = 5u;\n%s%s"
            "    out[i] = a ^ (b * 3u) ^ (c * 5u) ^ (d * 7u);\n"
            "}\n" % (name, name, NIN, NIN, decl, body))


def make_source(seed, count):
    rnd = random.Random(seed)
    g = Gen(rnd)
    names = ["k%d" % i for i in range(count)]
    src = "unsigned gpu_global_id(int dim);\n"
    for nm in names:
        src += g.kernel(nm) + "\n"
    return names, src


def compile_spirv(src, tmp, structure):
    f = os.path.join(tmp, "f.c")
    with open(f, "w") as fh:
        fh.write(src)
    out = os.path.join(tmp, "f_%d.spv" % structure)
    env = dict(os.environ)
    env["CRUST_SPIRV_STRUCT"] = "1" if structure else "0"
    env["CRUST_SPIRV_REPORT"] = "1"
    p = subprocess.run([sys.executable, "-m", "shivyc.main", "--target",
                        "spirv", f, "-o", out], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None, p.stdout + p.stderr
    return out, p.stderr


def native_run(names, src, inputs, tmp):
    gsrc = "\n".join(l for l in src.splitlines()
                     if not l.startswith("assert "))
    gsrc = gsrc.replace("unsigned gpu_global_id(int dim);", "")
    drv = ["#include <stdio.h>", "int main(void){",
           "static unsigned in[%d],out[%d];unsigned i;" % (2 * NIN, NIN)]
    drv.append("static const unsigned data[%d]={%s};" % (
        2 * NIN, ",".join("%du" % v for v in inputs)))
    for nm in names:
        drv.append("for(i=0;i<%d;i++)in[i]=data[i];" % (2 * NIN))
        drv.append("for(i=0;i<%d;i++)out[i]=0xdeadbeefu;" % NIN)
        drv.append("GPU_LAUNCH(1,1,1,%d,1,1,%s(in,out));" % (NIN, nm))
        drv.append('for(i=0;i<%d;i++)printf("%%u ",out[i]);'
                   'printf("\\n");' % NIN)
    drv.append("return 0;}")
    f = os.path.join(tmp, "n.c")
    with open(f, "w") as fh:
        fh.write('#include <crust_gpu.h>\n' + gsrc + "\n" + "\n".join(drv))
    exe = os.path.join(tmp, "n")
    p = subprocess.run(["gcc", "-O1", "-w", "-idirafter",
                        os.path.join(ROOT, "shivyc", "include"), f, "-o", exe],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("gcc failed:\n" + p.stderr)
    p = subprocess.run([exe], capture_output=True, text=True, timeout=60)
    rows = []
    for line in p.stdout.strip().split("\n"):
        rows.append([int(x) for x in line.split()])
    return rows


def spirv_run(path, names, inputs):
    import spirv_interp
    from shivyc.spirv import bytes_to_words
    with open(path, "rb") as fh:
        words = bytes_to_words(fh.read())
    from shivyc import spirv_reader
    errs = spirv_reader.validate(words)
    if errs:
        raise RuntimeError("invalid module: %s" % errs[:3])
    machine = spirv_interp.Machine(words)      # decode once per module
    rows = []
    for nm in names:
        out = bytearray(struct.pack("%dI" % NIN, *([0xdeadbeef] * NIN)))
        machine.run(nm, (1, 1, 1),
                    {0: bytearray(struct.pack("%dI" % (2 * NIN), *inputs)),
                     1: out})
        rows.append(list(struct.unpack("%dI" % NIN, bytes(out))))
    return rows


def run_batch(seed, count, verbose=False):
    names, src = make_source(seed, count)
    rnd = random.Random(seed ^ 0x5eed)
    inputs = [rnd.choice([0, 1, 2, 3, 7, 100, 0xffffffff, rnd.randint(0, 999)])
              for _ in range(2 * NIN)]
    with tempfile.TemporaryDirectory() as tmp:
        ref = native_run(names, src, inputs, tmp)
        stats = {"structured": 0, "dispatcher": 0}
        for structure in (1, 0):
            path, log = compile_spirv(src, tmp, structure)
            if path is None:
                return ["seed %d: compile failed (structure=%d):\n%s"
                        % (seed, structure, log)], stats, src
            if structure:
                for line in log.splitlines():
                    if "structured" in line and "__fallback__" not in line:
                        stats["structured"] += 1
                    elif "dispatcher" in line:
                        stats["dispatcher"] += 1
                if "__fallback__" in log:
                    return ["seed %d: structurizer output was invalid:\n%s"
                            % (seed, log)], stats, src
            got = spirv_run(path, names, inputs)
            for k in range(len(names)):
                if got[k] != ref[k]:
                    return ["seed %d kernel %s (structure=%d): got %s want %s"
                            % (seed, names[k], structure, got[k], ref[k])], \
                        stats, src
    return [], stats, src


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--kernels", type=int, default=100)
    ap.add_argument("--batch", type=int, default=25)
    a = ap.parse_args(argv)
    total = {"structured": 0, "dispatcher": 0}
    done = 0
    seed = a.seed
    bad = 0
    while done < a.kernels:
        n = min(a.batch, a.kernels - done)
        errs, stats, src = run_batch(seed, n)
        for k in total:
            total[k] += stats[k]
        if errs:
            bad += 1
            print("\n".join(errs))
            path = os.path.join(tempfile.gettempdir(),
                                "spirv_fuzz_fail_%d.c" % seed)
            with open(path, "w") as fh:
                fh.write(src)
            print("source saved to %s" % path)
            break
        done += n
        seed += 1
    print("kernels: %d  structured: %d  dispatcher fallback: %d  %s" % (
        done, total["structured"], total["dispatcher"],
        "FAILED" if bad else "ok"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
