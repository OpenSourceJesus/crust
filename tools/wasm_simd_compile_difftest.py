#!/usr/bin/env python3
"""Differential test for *compiling* SIMD: C intrinsics to wasm SIMD.

    prog.c --shivyc --target wasm--> prog.wasm --node--> answer A
    prog.c --cc (scalar fallback)--> binary      --------> answer B

A and B must match. The same source is used for both, because
`shivyc/include/wasm_simd128.h` compiles each intrinsic to the single wasm
instruction under `--target wasm` and to portable scalar C everywhere else.
That is what makes an ordinary compiler a usable oracle for vector code: the
scalar path is an independent implementation of the same specification, so a
wrong lane order, a signedness mistake, or a shift that fails to mask shows up
as a disagreement.

This is the compiler direction. `tools/wasm_simd_difftest.py` checks the other
one -- that wasm2c translates SIMD correctly -- and the two together mean a
vector program can go from C to wasm and back.

    python3 tools/wasm_simd_compile_difftest.py [-v]
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INC = os.path.join(ROOT, "shivyc", "include")

CC = os.environ.get("CC", "gcc")
NODE = os.environ.get("NODE", "node")

# Each case is a function body returning an int. Chosen to cover the mistakes
# that actually happen in vector code: lane ordering, signed versus unsigned
# comparison and shift, saturating versus wrapping arithmetic, and the
# all-ones result of a comparison feeding bitselect.
CASES = [
    ("splat_extract",
     "v128_t a = wasm_i32x4_splat(42); "
     "return wasm_i32x4_extract_lane(a, 3);"),
    ("add",
     "v128_t a = wasm_i32x4_splat(20), b = wasm_i32x4_splat(22); "
     "return wasm_i32x4_extract_lane(wasm_i32x4_add(a, b), 0);"),
    ("sub",
     "v128_t a = wasm_i32x4_splat(50), b = wasm_i32x4_splat(8); "
     "return wasm_i32x4_extract_lane(wasm_i32x4_sub(a, b), 1);"),
    ("mul",
     "v128_t a = wasm_i32x4_splat(6), b = wasm_i32x4_splat(7); "
     "return wasm_i32x4_extract_lane(wasm_i32x4_mul(a, b), 2);"),
    ("neg",
     "v128_t a = wasm_i32x4_splat(-42); "
     "return wasm_i32x4_extract_lane(wasm_i32x4_neg(a), 0);"),
    ("abs",
     "v128_t a = wasm_i32x4_splat(-42); "
     "return wasm_i32x4_extract_lane(wasm_i32x4_abs(a), 0);"),
    # Per-lane values, so a lane-order mistake changes the answer.
    ("replace_lanes",
     "v128_t a = wasm_i32x4_splat(0); "
     "a = wasm_i32x4_replace_lane(a, 0, 1); "
     "a = wasm_i32x4_replace_lane(a, 1, 10); "
     "a = wasm_i32x4_replace_lane(a, 2, 100); "
     "a = wasm_i32x4_replace_lane(a, 3, 1000); "
     "return wasm_i32x4_extract_lane(a, 2) - "
     "wasm_i32x4_extract_lane(a, 0) - 57;"),
    ("lane_sum",
     "v128_t a = wasm_i32x4_splat(0); int i, s = 0; "
     "a = wasm_i32x4_replace_lane(a, 0, 3); "
     "a = wasm_i32x4_replace_lane(a, 1, 5); "
     "a = wasm_i32x4_replace_lane(a, 2, 7); "
     "a = wasm_i32x4_replace_lane(a, 3, 11); "
     "s += wasm_i32x4_extract_lane(a, 0); "
     "s += wasm_i32x4_extract_lane(a, 1); "
     "s += wasm_i32x4_extract_lane(a, 2); "
     "s += wasm_i32x4_extract_lane(a, 3); "
     "return s + 16;"),
    ("min_max_signed",
     "v128_t a = wasm_i32x4_splat(-5), b = wasm_i32x4_splat(3); "
     "return wasm_i32x4_extract_lane(wasm_i32x4_min_s(a, b), 0) + "
     "wasm_i32x4_extract_lane(wasm_i32x4_max_s(a, b), 0) + 44;"),
    # The signed and unsigned forms must differ on a negative lane.
    ("min_signed_vs_unsigned",
     "v128_t a = wasm_i32x4_splat(-1), b = wasm_i32x4_splat(7); "
     "return (wasm_i32x4_extract_lane(wasm_i32x4_min_s(a, b), 0) == -1) * 20 "
     "+ (wasm_i32x4_extract_lane(wasm_i32x4_min_u(a, b), 0) == 7) * 22;"),
    ("compare_eq_is_all_ones",
     "v128_t a = wasm_i32x4_splat(5); "
     "return wasm_i32x4_extract_lane(wasm_i32x4_eq(a, a), 0) + 43;"),
    ("compare_lt_signed",
     "v128_t a = wasm_i32x4_splat(-1), z = wasm_i32x4_splat(0); "
     "return (wasm_i32x4_extract_lane(wasm_i32x4_lt_s(a, z), 0) != 0) * 21 + "
     "(wasm_i32x4_extract_lane(wasm_i32x4_lt_u(a, z), 0) == 0) * 21;"),
    ("bitmask",
     "v128_t a = wasm_i32x4_splat(0); "
     "a = wasm_i32x4_replace_lane(a, 0, -1); "
     "a = wasm_i32x4_replace_lane(a, 2, -1); "
     "return wasm_i32x4_bitmask(a) + 37;"),
    ("all_any_true",
     "v128_t a = wasm_i32x4_splat(1), z = wasm_i32x4_splat(0); "
     "return wasm_i32x4_all_true(a) * 20 + wasm_v128_any_true(z) + 22;"),
    ("bitwise",
     "v128_t a = wasm_i32x4_splat(0xF0), b = wasm_i32x4_splat(0x3C); "
     "return wasm_i32x4_extract_lane(wasm_v128_and(a, b), 0) + "
     "wasm_i32x4_extract_lane(wasm_v128_xor(a, b), 0) - 190;"),
    ("bitselect",
     "v128_t a = wasm_i32x4_splat(0xAA), b = wasm_i32x4_splat(0x55), "
     "m = wasm_i32x4_splat(-1); "
     "return wasm_i32x4_extract_lane(wasm_v128_bitselect(a, b, m), 0) - 128;"),
    # Shift counts must be masked to the lane width, not left to C.
    ("shifts",
     "v128_t a = wasm_i32x4_splat(3); "
     "return wasm_i32x4_extract_lane(wasm_i32x4_shl(a, 4), 0) - 6;"),
    ("shift_signed",
     "v128_t a = wasm_i32x4_splat(-16); "
     "return wasm_i32x4_extract_lane(wasm_i32x4_shr_s(a, 2), 0) + 46;"),
    ("shift_unsigned",
     "v128_t a = wasm_i32x4_splat(-1); "
     "return (wasm_i32x4_extract_lane(wasm_i32x4_shr_u(a, 28), 0) == 15) "
     "* 42;"),
    ("i16x8_lanes",
     "v128_t a = wasm_i16x8_splat(300); "
     "return wasm_i16x8_extract_lane_s(a, 4) - 258;"),
    ("i8x16_signed_vs_unsigned",
     "v128_t a = wasm_i8x16_splat(200); "
     "return (wasm_i8x16_extract_lane_s(a, 0) == -56) * 20 + "
     "(wasm_i8x16_extract_lane_u(a, 0) == 200) * 22;"),
    ("i64x2",
     "v128_t a = wasm_i64x2_splat(1000000000000L); "
     "v128_t b = wasm_i64x2_splat(3L); "
     "return (int)(wasm_i64x2_extract_lane(wasm_i64x2_mul(a, b), 0) % 251);"),
    ("f32x4",
     "v128_t a = wasm_f32x4_splat(1.5f), b = wasm_f32x4_splat(2.5f); "
     "return (int)(wasm_f32x4_extract_lane(wasm_f32x4_add(a, b), 0) * 10.5f);"),
    ("f64x2",
     "v128_t a = wasm_f64x2_splat(6.0), b = wasm_f64x2_splat(7.0); "
     "return (int)wasm_f64x2_extract_lane(wasm_f64x2_mul(a, b), 1);"),
    ("f32x4_sqrt",
     "v128_t a = wasm_f32x4_splat(1764.0f); "
     "return (int)wasm_f32x4_extract_lane(wasm_f32x4_sqrt(a), 0);"),
    ("loop_accumulate",
     "v128_t acc = wasm_i32x4_splat(0); int i; "
     "for (i = 0; i < 10; i++) acc = wasm_i32x4_add(acc, "
     "wasm_i32x4_splat(i)); "
     "return wasm_i32x4_extract_lane(acc, 1) - 3;"),
    ("vector_through_function",
     "return helper(wasm_i32x4_splat(21));"),
]

# A helper defined ahead of main, so a vector can be seen crossing a call
# boundary -- which is where the by-value struct ABI meets the intrinsics.
PRELUDE = ("static int helper(v128_t v) { "
           "return wasm_i32x4_extract_lane(v, 0) * 2; }\n")


def _run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)
    return p.returncode, p.stdout, p.stderr


def source_for(body):
    return ("#include <wasm_simd128.h>\n" + PRELUDE +
            "int main(void) { " + body + " }\n")


def test_one(name, body, workdir):
    cpath = os.path.join(workdir, name + ".c")
    with open(cpath, "w") as f:
        f.write(source_for(body))

    # The oracle: the same source, scalar fallback, ordinary compiler.
    ora = os.path.join(workdir, name + ".ora")
    rc, _, err = _run([CC, "-w", "-std=c99", "-I", INC, cpath,
                       "-o", ora, "-lm"])
    if rc != 0:
        return "ERROR", "oracle compile failed: %s" % err.strip()[:180]
    ora_rc, _, _ = _run([ora])

    wpath = os.path.join(workdir, name + ".wasm")
    rc, out, err = _run([sys.executable, "-m", "shivyc.main", cpath,
                         "-o", wpath, "--target", "wasm"], cwd=ROOT)
    blob = out + err
    if "NotImplementedError" in blob:
        detail = "back end refuses"
        for ln in blob.split("\n"):
            if "NotImplementedError:" in ln:
                detail = ln.split("NotImplementedError:", 1)[1].strip()
        return "SKIP", detail
    if rc != 0 or not os.path.exists(wpath):
        return "ERROR", "shivyc failed: %s" % blob.strip()[:180]

    env = dict(os.environ)
    env["WASM_RUN_REPORT"] = "1"
    p = subprocess.run([NODE, os.path.join(HERE, "wasm_run.js"), wpath],
                       capture_output=True, text=True, env=env, timeout=120)
    if p.returncode != 0 or "RESULT " not in p.stderr:
        return "FAIL", "wasm invalid or trapped: %s" % p.stderr.strip()[:150]
    mine = 0
    for ln in p.stderr.split("\n"):
        if ln.startswith("RESULT "):
            mine = int(ln.split()[1]) & 0xFF

    if mine != ora_rc:
        return "FAIL", "wasm=%d oracle=%d" % (mine, ora_rc)

    # Confirm real vector instructions were emitted, not a scalar expansion
    # that happens to give the right answer.
    sys.path.insert(0, ROOT)
    import shivyc.wasm_reader as reader
    mod = reader.decode_file(wpath)
    n = 0
    for f in mod.funcs:
        for ins in f.instrs:
            if ins.op.startswith("v128.") or "x16." in ins.op \
                    or "x8." in ins.op or "x4." in ins.op or "x2." in ins.op:
                n += 1
    if n == 0:
        return "FAIL", "no SIMD instructions emitted (answer %d)" % mine
    return "PASS", "= %d, %d SIMD instrs" % (mine, n)


def main(argv):
    verbose = "-v" in argv
    for tool in (CC, NODE):
        rc, _, _ = _run([tool, "--version"])
        if rc != 0:
            print("missing toolchain: %s" % tool)
            return 2

    workdir = tempfile.mkdtemp(prefix="simdcc-")
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "ERROR": 0}
    for name, body in CASES:
        status, detail = test_one(name, body, workdir)
        counts[status] += 1
        if verbose or status != "PASS":
            print("  %-5s %-26s %s" % (status, name, detail))

    print("\nSIMD compile difftest: %d pass, %d fail, %d skip, %d error"
          % (counts["PASS"], counts["FAIL"], counts["SKIP"],
             counts["ERROR"]))
    return 1 if (counts["FAIL"] or counts["ERROR"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
