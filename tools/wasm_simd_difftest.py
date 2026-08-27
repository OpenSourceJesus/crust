#!/usr/bin/env python3
"""Differential test for the SIMD half of wasm2c.

Compiling is not the same as being right. These build small SIMD modules
directly with the encoder, then check that the module and its wasm2c
translation agree:

    module --node------------------> answer A
    module --wasm2c--> C --cc------> answer B

A and B must match. Node's SIMD is an independent implementation of the
specification, so a lane-order mistake, a wrong signedness, or a saturation
that clamps at the wrong bound shows up as a disagreement rather than as
plausible-looking output.

The modules are built here rather than compiled from C because Crust's own
back end does not emit SIMD -- so this is the only way to exercise it, and it
has the useful side effect of also testing the encoder's SIMD support.

    python3 tools/wasm_simd_difftest.py
    python3 tools/wasm_simd_difftest.py -v
"""
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import shivyc.wasm as w                                       # noqa: E402
import shivyc.wasm_simd as simd                               # noqa: E402

CC = os.environ.get("CC", "gcc")
NODE = os.environ.get("NODE", "node")


def simd_op(body, name, *imm):
    """Emit one SIMD instruction into a FuncBody.

    The encoder has no SIMD support of its own -- the back end never emits
    any -- so this writes the 0xFD prefix and the operator's index directly,
    looked up in the same table the decoder uses.
    """
    code = None
    for c in simd.OPCODES:
        if simd.OPCODES[c][0] == name:
            code = c
            break
    if code is None:
        raise KeyError("no such SIMD operator: %s" % name)
    body.code.append(0xFD)
    body.code.extend(w.uleb(code))
    kind = simd.OPCODES[code][1]
    if kind == simd.IMM_MEMARG:
        body.code.extend(w.uleb(imm[0]))
        body.code.extend(w.uleb(imm[1]))
    elif kind == simd.IMM_MEMARG_LANE:
        body.code.extend(w.uleb(imm[0]))
        body.code.extend(w.uleb(imm[1]))
        body.code.append(imm[2])
    elif kind == simd.IMM_LANE:
        body.code.append(imm[0])
    elif kind in (simd.IMM_V128, simd.IMM_SHUFFLE):
        for b in imm[0]:
            body.code.append(b)


def v128_const(vals, fmt):
    """Sixteen immediate bytes from a list of lane values."""
    return list(struct.pack("<" + fmt * len(vals), *vals))


def build(name, lanes_a, lanes_b, fmt, op, extract, lane=0):
    """A module computing `op` on two constant vectors and returning one lane.

    Returning a single i32 lane keeps the comparison simple: both sides
    produce one number, and any lane-order or width mistake changes it.
    """
    mod = w.WasmModule()
    mod.declare_func("run", [], [w.I32])
    b = w.FuncBody()
    simd_op(b, "v128.const", v128_const(lanes_a, fmt))
    if lanes_b is not None:
        simd_op(b, "v128.const", v128_const(lanes_b, fmt))
    simd_op(b, op)
    simd_op(b, extract, lane)
    mod.set_body("run", b)
    mod.export_func("run")
    return w.module_bytes(mod)


# (name, lane values A, lane values B, struct format, operator, extractor,
#  lane index). Chosen to catch the mistakes that actually happen: signed vs
#  unsigned comparison and shift, saturation bounds, lane ordering, and the
#  all-ones result of a vector comparison.
CASES = [
    ("i32x4_add", [1, 2, 3, 4], [10, 20, 30, 40], "i",
     "i32x4.add", "i32x4.extract_lane", 2),
    ("i32x4_sub", [100, 200, 300, 400], [1, 2, 3, 4], "i",
     "i32x4.sub", "i32x4.extract_lane", 3),
    ("i32x4_mul", [3, 5, 7, 11], [13, 17, 19, 23], "i",
     "i32x4.mul", "i32x4.extract_lane", 1),
    ("i32x4_neg", [5, -6, 7, -8], None, "i",
     "i32x4.neg", "i32x4.extract_lane", 1),
    ("i32x4_abs", [-5, 6, -7, 8], None, "i",
     "i32x4.abs", "i32x4.extract_lane", 0),
    ("i32x4_min_s", [-5, 6, -7, 8], [3, -4, 5, -6], "i",
     "i32x4.min_s", "i32x4.extract_lane", 0),
    ("i32x4_min_u", [-5, 6, -7, 8], [3, -4, 5, -6], "i",
     "i32x4.min_u", "i32x4.extract_lane", 0),
    ("i32x4_max_s", [-5, 6, -7, 8], [3, -4, 5, -6], "i",
     "i32x4.max_s", "i32x4.extract_lane", 1),
    # A true lane must be all ones, not 1: bitselect and bitmask depend on it.
    ("i32x4_eq", [1, 2, 3, 4], [1, 0, 3, 0], "i",
     "i32x4.eq", "i32x4.extract_lane", 0),
    ("i32x4_lt_s", [-1, 2, -3, 4], [0, 0, 0, 0], "i",
     "i32x4.lt_s", "i32x4.extract_lane", 0),
    ("i32x4_lt_u", [-1, 2, -3, 4], [0, 0, 0, 0], "i",
     "i32x4.lt_u", "i32x4.extract_lane", 0),
    ("i16x8_add", [1, 2, 3, 4, 5, 6, 7, 8],
     [100, 200, 300, 400, 500, 600, 700, 800], "h",
     "i16x8.add", "i16x8.extract_lane_s", 5),
    ("i16x8_add_sat_s", [32000, 1, 2, 3, 4, 5, 6, 7],
     [32000, 1, 2, 3, 4, 5, 6, 7], "h",
     "i16x8.add_sat_s", "i16x8.extract_lane_s", 0),
    ("i16x8_sub_sat_s", [-32000, 1, 2, 3, 4, 5, 6, 7],
     [32000, 1, 2, 3, 4, 5, 6, 7], "h",
     "i16x8.sub_sat_s", "i16x8.extract_lane_s", 0),
    ("i8x16_add", list(range(16)), [1] * 16, "b",
     "i8x16.add", "i8x16.extract_lane_s", 15),
    ("i8x16_add_sat_u", [200] * 16, [100] * 16, "B",
     "i8x16.add_sat_u", "i8x16.extract_lane_u", 0),
    ("i8x16_extract_u", [-1] + [0] * 15, None, "b",
     "i8x16.popcnt", "i8x16.extract_lane_u", 0),
    ("v128_and", [0xF0F0F0F0, 0, 0, 0], [0x3C3C3C3C, 0, 0, 0], "I",
     "v128.and", "i32x4.extract_lane", 0),
    ("v128_or", [0xF0F0F0F0, 0, 0, 0], [0x3C3C3C3C, 0, 0, 0], "I",
     "v128.or", "i32x4.extract_lane", 0),
]


def _run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return p.returncode, p.stdout, p.stderr


NODE_RUNNER = r"""
const fs = require('fs');
(async () => {
  const m = await WebAssembly.compile(fs.readFileSync(process.argv[2]));
  const i = await WebAssembly.instantiate(m, {});
  process.stdout.write(String(i.exports.run() | 0));
})();
"""

C_MAIN = r"""
#include <stdio.h>
extern u32 w2c_export_run(void);
int wasm2c_main(void) { printf("%d", (int)(s32)w2c_export_run()); return 0; }
"""


def test_one(name, blob, workdir, runner):
    wpath = os.path.join(workdir, name + ".wasm")
    with open(wpath, "wb") as f:
        f.write(blob)

    rc, out, err = _run([NODE, runner, wpath])
    if rc != 0:
        return "ERROR", "node rejected the module: %s" % err.strip()[:120]
    expected = out.strip()

    cpath = os.path.join(workdir, name + ".c")
    rc, out, err = _run([sys.executable, os.path.join(HERE, "wasm2c.py"),
                         wpath, "-o", cpath])
    if rc != 0:
        return "FAIL", "wasm2c failed: %s" % (out + err).strip()[:160]

    # The generated file has its own main(); replace it with one that prints
    # the exported result so the two sides are comparable.
    with open(cpath) as f:
        text = f.read()
    text = text[:text.index("int main(int argc, char **argv)")]
    text += ("#include <stdio.h>\nint main(void){ wasm_init();"
             " printf(\"%d\", (int)(s32)w2c_export_run()); return 0; }\n")
    with open(cpath, "w") as f:
        f.write(text)

    binp = os.path.join(workdir, name + ".bin")
    rc, _, err = _run([CC, "-w", "-std=c99", "-I", HERE, cpath,
                       "-o", binp, "-lm"])
    if rc != 0:
        return "FAIL", "translated C did not compile: %s" % err.strip()[:200]
    rc, got, err = _run([binp])
    got = got.strip()
    if got != expected:
        return "FAIL", "node=%s wasm2c=%s" % (expected, got)
    return "PASS", "= %s" % expected


def main(argv):
    verbose = "-v" in argv
    for tool in (NODE, CC):
        rc, _, _ = _run([tool, "--version"])
        if rc != 0:
            print("missing toolchain: %s" % tool)
            return 2

    problems = simd.self_check()
    if problems:
        for p in problems:
            print("  TABLE %s" % p)
        return 1

    import shivyc.wasm_simd_c as simd_c
    missing = simd_c.coverage()
    if missing:
        print("  %d SIMD operator(s) have no translation: %s"
              % (len(missing), ", ".join(missing[:8])))

    workdir = tempfile.mkdtemp(prefix="simddiff-")
    runner = os.path.join(workdir, "run.js")
    with open(runner, "w") as f:
        f.write(NODE_RUNNER)

    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0}
    for entry in CASES:
        name, a, b, fmt, op, extract, lane = entry
        try:
            blob = build(name, a, b, fmt, op, extract, lane)
        except Exception as e:
            counts["ERROR"] += 1
            print("  ERROR %-20s could not build: %s" % (name, e))
            continue
        status, detail = test_one(name, blob, workdir, runner)
        counts[status] += 1
        if verbose or status != "PASS":
            print("  %-5s %-20s %s" % (status, name, detail))

    print("\nSIMD difftest: %d pass, %d fail, %d error  "
          "(%d opcodes decoded, %d translated)"
          % (counts["PASS"], counts["FAIL"], counts["ERROR"],
             len(simd.OPCODES), len(simd_c.HANDLERS)))
    return 1 if (counts["FAIL"] or counts["ERROR"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
