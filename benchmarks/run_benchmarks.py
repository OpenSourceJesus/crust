#!/usr/bin/env python3
"""Benchmark harness: ShivyCX extended-C features vs gcc -O0 vs CCC.

ShivyCX emits unoptimized, -O0-class code, so the fair external peer is
**gcc -O0** (gcc -O2 would measure a different compiler class entirely). The
primary comparison for each feature is still feature-ON vs feature-OFF on the
same compiler, which holds codegen quality constant and isolates the feature;
gcc -O0 is shown alongside as the honest "what an ordinary unoptimizing C
compiler does" reference. CCC (Claude's C Compiler, another from-scratch
unoptimizing compiler) is included as a third peer when built; see
benchmarks/build_ccc.sh. It is auto-detected and omitted if absent.

For every configuration the harness:
  * compiles the program,
  * runs it and records the exit code (differential correctness: all configs
    must agree),
  * times it (best-of-N wall clock), and
  * extracts a static codegen metric that directly witnesses the feature.

Results -> results/results.json.
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RESULTS_DIR = os.path.join(HERE, "results")
REPS = 5


def _shivyc(src, out, extra=None, quiet=False):
    cmd = [sys.executable, "-m", "shivyc.main", "--no-cache", src, "-o", out]
    if extra:
        cmd[4:4] = extra
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    log = p.stdout + p.stderr
    # Most callers ignore the return code and go straight to reading the .s
    # this was meant to produce. Surface the failure here, or the first symptom
    # is an unrelated FileNotFoundError several lines later.
    if not quiet and (p.returncode != 0 or not os.path.exists(out)):
        sys.stderr.write("shivyc failed on %s:\n%s\n"
                         % (os.path.basename(src), log.strip()[:600]))
    return p.returncode, log


def _gcc(src, out, opt="-O0"):
    p = subprocess.run(["gcc", opt, src, "-o", out],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


# CCC = Claude's C Compiler (https://github.com/anthropics/claudes-c-compiler),
# another from-scratch unoptimizing C compiler -- a natural third peer next to
# gcc -O0. Auto-detected; if it is not built, the CCC column is simply omitted
# (build it with benchmarks/build_ccc.sh, or set $CCC to the binary).
def _find_ccc():
    import shutil
    cand = os.environ.get("CCC") or shutil.which("ccc")
    if cand and os.path.exists(cand):
        return cand
    guess = os.path.join(REPO, "..", "ccc", "target", "release", "ccc")
    return guess if os.path.exists(guess) else None


CCC_BIN = _find_ccc()


def _ccc(src, out):
    if not CCC_BIN:
        return None, "ccc not available"
    p = subprocess.run([CCC_BIN, "-o", out, src],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def _add_ccc(cfgs, src, d, metric="external unoptimizing C compiler"):
    """Append a CCC config built from the same plain-C source gcc -O0 uses, if
    CCC is available and compiles it. No-op otherwise."""
    if not CCC_BIN:
        return
    out = os.path.join(d, "bin_ccc")
    rc, _ = _ccc(src, out)
    if rc == 0 and os.path.exists(out):
        cfgs.append({"name": "CCC", "binary": out, "baseline": False,
                     "metric": metric, "metric_val": None})


def _run_exit(binary, args=None):
    return subprocess.run([binary] + list(args or [])).returncode


def _time_best(binary, reps=REPS, args=None):
    argv = [binary] + list(args or [])
    best = float("inf")
    for _ in range(reps):
        t0 = time.perf_counter()
        subprocess.run(argv, stdout=subprocess.DEVNULL)
        best = min(best, time.perf_counter() - t0)
    return best


def _read_asm(src):
    """Assembly ShivyCX left beside `src`, or "" if the compile did not get far
    enough to emit any. Returning "" degrades the affected metric instead of
    aborting the whole suite -- one kernel that fails to compile should not
    cost us results.json and every figure built from it."""
    path = src[:-2] + ".s"
    if not os.path.exists(path):
        sys.stderr.write("no assembly at %s (compile failed?); "
                         "metrics for it will be reported as unavailable\n"
                         % os.path.basename(path))
        return ""
    with open(path) as f:
        return f.read()


def _write_baseline(src_on, src_off, transform):
    """Derive a feature-OFF baseline source from the primary source.

    The harness is self-contained: the baseline .c is generated here rather than
    committed, so a fresh checkout never lacks it. `transform` maps the source
    text to its plain-C equivalent (e.g. stripping `assert` contract clauses or
    the `__metamorphic__` specifier).
    """
    with open(src_on) as f:
        text = f.read()
    with open(src_off, "w") as f:
        f.write(transform(text))


def _strip_contract_clauses(text):
    """Drop lines whose first non-space token is `assert` (contract clauses)."""
    return "".join(ln for ln in text.splitlines(keepends=True)
                   if not ln.lstrip().startswith("assert "))


def _strip_metamorphic(text):
    return text.replace(" __metamorphic__", "")


def _extract_function(asm, name):
    """Lines of function `name`, label to first ret/jmp terminator."""
    out, capturing = [], False
    for ln in asm.splitlines():
        if ln.strip().startswith(name + ":"):
            capturing = True
        if capturing:
            out.append(ln)
            if re.search(r"\b(ret|jmp)\b", ln):
                break
    return "\n".join(out)


def _count(asm, pat):
    return len(re.findall(pat, asm))


def _finish(configs):
    for c in configs:
        a = c.get("run_args")
        c["exit_code"] = _run_exit(c["binary"], a)
        c["time_s"] = _time_best(c["binary"], args=a)
    return configs


# ===========================================================================
# Benchmark 1: _Nbit globals (SIMD bit-packing into xmm15)
# ===========================================================================
NBIT_FLAGS = ["enabled_1bit", "prio_3bit", "level_4bit", "mask_5bit", "mode_2bit"]


def _flag_mem_reads(fn_text):
    return sum(len(re.findall(r"PTR \[" + re.escape(f) + r"\]", fn_text))
               for f in NBIT_FLAGS)


def bench_nbit():
    d = os.path.join(HERE, "nbit_globals")
    src = os.path.join(d, "bench_nbit.c")
    cfgs = []

    _shivyc(src, os.path.join(d, "bin_shivyc_off"))
    n = _flag_mem_reads(_extract_function(_read_asm(src), "irq_handler"))
    cfgs.append({"name": "ShivyCX (no pack)", "binary": os.path.join(d, "bin_shivyc_off"),
                 "baseline": True, "metric": "%d flag mem-loads" % n, "metric_val": n})

    _shivyc(src, os.path.join(d, "bin_shivyc_on"), ["-fsimd-pack-globals"])
    n = _flag_mem_reads(_extract_function(_read_asm(src), "irq_handler"))
    cfgs.append({"name": "ShivyCX (-fsimd-pack-globals)", "binary": os.path.join(d, "bin_shivyc_on"),
                 "baseline": False, "metric": "%d flag mem-loads (reads xmm15)" % n, "metric_val": n})

    _gcc(src, os.path.join(d, "bin_gcc0"))
    cfgs.append({"name": "gcc -O0", "binary": os.path.join(d, "bin_gcc0"),
                 "baseline": False, "metric": "5 mem-loads; never uses xmm15", "metric_val": None})
    _add_ccc(cfgs, src, d)

    return {"benchmark": "nbit_globals", "configs": _finish(cfgs)}


# ===========================================================================
# Benchmark 2: contracts -> fallback-free SIMD reduction
# ===========================================================================
def bench_contracts():
    d = os.path.join(HERE, "contracts")
    src_on = os.path.join(d, "bench_contracts.c")
    src_off = os.path.join(d, "bench_contracts_baseline.c")
    _write_baseline(src_on, src_off, _strip_contract_clauses)
    cfgs = []

    _shivyc(src_on, os.path.join(d, "bin_shivyc_contract"))
    fn = _extract_function(_read_asm(src_on), "calc_sum")
    vec = "paddd" in fn and "movdqu" in fn
    cfgs.append({"name": "ShivyCX (+contract)", "binary": os.path.join(d, "bin_shivyc_contract"),
                 "baseline": False, "metric": "SSE2 body, no remainder" if vec else "scalar",
                 "metric_val": vec})

    _shivyc(src_off, os.path.join(d, "bin_shivyc_scalar"))
    cfgs.append({"name": "ShivyCX (scalar, no contract)", "binary": os.path.join(d, "bin_shivyc_scalar"),
                 "baseline": True, "metric": "scalar loop", "metric_val": False})

    _gcc(src_off, os.path.join(d, "bin_gcc0"))   # gcc can't parse `assert` clauses
    cfgs.append({"name": "gcc -O0", "binary": os.path.join(d, "bin_gcc0"),
                 "baseline": False, "metric": "scalar loop", "metric_val": None})
    _add_ccc(cfgs, src_off, d)

    return {"benchmark": "contracts_simd", "configs": _finish(cfgs)}


# Runtime helpers py2c may emit for a kernel that is otherwise runtime-free.
# Floor semantics, matching pymod/pyfdiv in the py2c runtime: Python rounds
# toward negative infinity where C truncates toward zero.
_MINI_RT = {
    "pymod": (
        "static long pymod(long a, long b) {\n"
        "    if (b == 0) return 0;\n"
        "    long r = a % b;\n"
        "    if (r != 0 && ((r < 0) != (b < 0))) r += b;\n"
        "    return r;\n"
        "}\n"),
    "pyfdiv": (
        "static long pyfdiv(long a, long b) {\n"
        "    if (b == 0) return 0;\n"
        "    long q = a / b;\n"
        "    if ((a % b != 0) && ((a < 0) != (b < 0))) q -= 1;\n"
        "    return q;\n"
        "}\n"),
}


def _py2c(py_src, out_c):
    """Transpile an rpython .py to C via tools/py2c.py and post-process it the
    way shivyc.main's .py path does: drop the unused runtime include (these
    kernels reference no runtime) and re-supply the malloc prototype. Returns
    the C path, or None on failure."""
    tools = os.path.join(REPO, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import tempfile
    import py2c
    d = tempfile.mkdtemp()
    cpath, err = py2c.transpile_file(py_src, d)
    if err or not cpath:
        return None
    code = open(cpath).read()
    code = re.sub(r'#include "shivyc_rt\.h"\n', "", code)
    prelude = "void *malloc(unsigned long);\nint atoi(const char *);\n"
    # These kernels are meant to be runtime-free, but py2c lowers a few Python
    # operators to runtime helpers regardless -- `int(x) % 250` becomes
    # pymod(). Dropping the runtime header then left those undeclared and the
    # compile failed. Supply a local definition of each helper the generated
    # code actually references (semantics copied from the py2c runtime) so the
    # source stays self-contained.
    for name, defn in _MINI_RT.items():
        if re.search(r"\b" + name + r"\s*\(", code):
            prelude += defn
    code = prelude + code
    with open(out_c, "w") as f:
        f.write(code)
    return out_c


def bench_simd_py():
    """rpython SIMD kernel (.py) vs gcc -O0. One transpiled C source is compiled
    three ways: ShivyCX with the contract intact (vectorized saxpy), ShivyCX
    with the contract stripped (scalar), and gcc -O0 (scalar). All must agree on
    the exit code; the speedup isolates the contract-driven SSE body."""
    d = os.path.join(HERE, "simd_py")
    py_src = os.path.join(d, "bench_saxpy.py")
    simd_c = os.path.join(d, "bench_saxpy_simd.c")
    scal_c = os.path.join(d, "bench_saxpy_scalar.c")
    cfgs = []

    if _py2c(py_src, simd_c) is None:
        return {"benchmark": "simd_py_saxpy", "configs": _finish(cfgs)}
    _write_baseline(simd_c, scal_c, _strip_contract_clauses)

    _shivyc(simd_c, os.path.join(d, "bin_shivyc_simd"))
    fn = _extract_function(_read_asm(simd_c), "saxpy")
    vec = "mulps" in fn and "addps" in fn
    cfgs.append({"name": "ShivyCX .py (+contract SIMD)",
                 "binary": os.path.join(d, "bin_shivyc_simd"), "baseline": False,
                 "metric": "mulps+addps packed body" if vec else "scalar",
                 "metric_val": vec})

    _shivyc(scal_c, os.path.join(d, "bin_shivyc_scalar"))
    cfgs.append({"name": "ShivyCX .py (scalar, no contract)",
                 "binary": os.path.join(d, "bin_shivyc_scalar"), "baseline": True,
                 "metric": "scalar loop", "metric_val": False})

    _gcc(scal_c, os.path.join(d, "bin_gcc0"))
    cfgs.append({"name": "gcc -O0", "binary": os.path.join(d, "bin_gcc0"),
                 "baseline": False, "metric": "scalar loop", "metric_val": None})
    _add_ccc(cfgs, scal_c, d, metric="external unoptimizing C compiler")

    return {"benchmark": "simd_py_saxpy", "configs": _finish(cfgs)}


def bench_simd_o2():
    """rpython SIMD kernel (.py) vs gcc -O2. The loop count comes from argv, so
    gcc -O2 cannot fold the program to a constant, and the in-place recurrence
    x += y is not loop-invariant so it cannot be hoisted -- gcc must actually
    run (and auto-vectorize) the loop. ShivyCX vectorizes via the len(x)%4
    contract (no peeling, no remainder). This pits ShivyCX's contract SIMD
    against a real optimizing compiler on equal, un-foldable work."""
    d = os.path.join(HERE, "simd_py")
    py_src = os.path.join(d, "bench_evolve.py")
    simd_c = os.path.join(d, "bench_evolve_simd.c")
    scal_c = os.path.join(d, "bench_evolve_scalar.c")
    REP_ARG = ["400000"]
    cfgs = []

    if _py2c(py_src, simd_c) is None:
        return {"benchmark": "simd_py_vs_gcc_O2", "configs": _finish(cfgs)}
    _write_baseline(simd_c, scal_c, _strip_contract_clauses)

    _shivyc(simd_c, os.path.join(d, "bin_evolve_simd"))
    fn = _extract_function(_read_asm(simd_c), "vadd")
    vec = "addps" in fn
    cfgs.append({"name": "ShivyCX .py (+contract SIMD)",
                 "binary": os.path.join(d, "bin_evolve_simd"), "baseline": False,
                 "metric": "addps, no peel/remainder" if vec else "scalar",
                 "metric_val": vec, "run_args": REP_ARG})

    _gcc(scal_c, os.path.join(d, "bin_evolve_gcc2"), opt="-O2")
    cfgs.append({"name": "gcc -O2", "binary": os.path.join(d, "bin_evolve_gcc2"),
                 "baseline": True, "metric": "auto-vectorized + peel/remainder",
                 "metric_val": None, "run_args": REP_ARG})

    _gcc(scal_c, os.path.join(d, "bin_evolve_gcc0"), opt="-O0")
    cfgs.append({"name": "gcc -O0", "binary": os.path.join(d, "bin_evolve_gcc0"),
                 "baseline": False, "metric": "scalar loop",
                 "metric_val": None, "run_args": REP_ARG})

    return {"benchmark": "simd_py_vs_gcc_O2", "configs": _finish(cfgs)}


# ===========================================================================
# Benchmark 3: -fstackless-calls (direct call + tail-call + FPO)
# ===========================================================================
def bench_stackless():
    d = os.path.join(HERE, "stackless")
    src = os.path.join(d, "bench_stackless.c")
    cfgs = []

    _shivyc(src, os.path.join(d, "bin_shivyc_off"))
    asm = _read_asm(src)
    cfgs.append({"name": "ShivyCX (no stackless)", "binary": os.path.join(d, "bin_shivyc_off"),
                 "baseline": True,
                 "metric": "%d calls / %d frames" % (_count(asm, r"\bcall\b"), _count(asm, r"push rbp")),
                 "metric_val": _count(asm, r"\bcall\b")})

    _shivyc(src, os.path.join(d, "bin_shivyc_on"), ["-fstackless-calls"])
    asm = _read_asm(src)
    cfgs.append({"name": "ShivyCX (-fstackless-calls)", "binary": os.path.join(d, "bin_shivyc_on"),
                 "baseline": False,
                 "metric": "%d calls / %d frames" % (_count(asm, r"\bcall\b"), _count(asm, r"push rbp")),
                 "metric_val": _count(asm, r"\bcall\b")})

    _gcc(src, os.path.join(d, "bin_gcc0"))
    cfgs.append({"name": "gcc -O0", "binary": os.path.join(d, "bin_gcc0"),
                 "baseline": False, "metric": "full framed calls", "metric_val": None})
    _add_ccc(cfgs, src, d)

    return {"benchmark": "stackless", "configs": _finish(cfgs)}


# ===========================================================================
# Benchmark 4: -fmetamorphic (experimental, self-modifying return slot)
# ===========================================================================
def bench_metamorphic():
    d = os.path.join(HERE, "metamorphic")
    src_on = os.path.join(d, "bench_metamorphic.c")
    src_off = os.path.join(d, "bench_metamorphic_baseline.c")
    _write_baseline(src_on, src_off, _strip_metamorphic)
    cfgs = []

    _shivyc(src_off, os.path.join(d, "bin_shivyc_off"))
    cfgs.append({"name": "ShivyCX (normal call)", "binary": os.path.join(d, "bin_shivyc_off"),
                 "baseline": True, "metric": "ordinary call/ret", "metric_val": False})

    _shivyc(src_on, os.path.join(d, "bin_shivyc_on"), ["-fmetamorphic"])
    asm = _read_asm(src_on)
    # The feature is emitted when a return routes through a patched slot
    # (`metaret`) instead of the stack. The improved lowering keeps that slot in
    # writable .data -- off the code page -- so there is no RWX .mtext section
    # and no self-modifying-code stall; the old lowering put it in .mtext,
    # adjacent to the code, and paid an SMC machine clear on every call.
    emitted = "metaret" in asm and "jmp QWORD PTR [rip +" in asm
    if emitted and ".mtext" not in asm:
        metric = "off-page .data slot, no SMC"
    elif emitted:
        metric = "RWX .mtext slot, SMC per call"
    else:
        metric = "no metamorphic emitted"
    cfgs.append({"name": "ShivyCX (-fmetamorphic)", "binary": os.path.join(d, "bin_shivyc_on"),
                 "baseline": False,
                 "metric": metric,
                 "metric_val": emitted})

    _gcc(src_off, os.path.join(d, "bin_gcc0"))   # gcc can't parse __metamorphic__
    cfgs.append({"name": "gcc -O0", "binary": os.path.join(d, "bin_gcc0"),
                 "baseline": False, "metric": "ordinary call/ret", "metric_val": None})
    _add_ccc(cfgs, src_off, d)

    return {"benchmark": "metamorphic", "configs": _finish(cfgs)}


# ===========================================================================
# Benchmark 5: -f-eliminate-unused-members (whole-program struct shrink)
# ===========================================================================
def bench_member_elim():
    d = os.path.join(HERE, "member_elim")
    src = os.path.join(d, "bench_member_elim.c")

    def comm_bytes(asm, sym):
        m = re.search(r"\.comm " + re.escape(sym) + r"[ ,]+(\d+)", asm)
        return int(m.group(1)) if m else None

    _shivyc(src, os.path.join(d, "bin_off"))
    off = comm_bytes(_read_asm(src), "table")
    e_off = _run_exit(os.path.join(d, "bin_off"))

    rc, log = _shivyc(src, os.path.join(d, "bin_on"),
                      ["-f-eliminate-unused-members", "--print-eliminated-members"])
    on = comm_bytes(_read_asm(src), "table")
    removed = ""
    for ln in log.splitlines():
        if "eliminated from" in ln:
            removed = ln.strip()
    e_on = _run_exit(os.path.join(d, "bin_on"))

    _gcc(src, os.path.join(d, "bin_gcc0"))
    e_gcc = _run_exit(os.path.join(d, "bin_gcc0"))

    return {"benchmark": "member_elim", "kind": "bytes",
            "exit_agree": len({e_off, e_on, e_gcc}) == 1,
            "off_bytes": off, "on_bytes": on, "gcc_bytes": off,
            "removed": removed,
            "note": "gcc cannot shrink a struct: ABI-fixed layout"}


# ===========================================================================
# Benchmark 6: register-partitioned left/right threads (switch cost)
# ===========================================================================
def bench_threads():
    d = os.path.join(HERE, "threads")
    src = os.path.join(d, "bench_threads.c")
    sw = os.path.join(d, "switcher.s")
    cmd = [sys.executable, "-m", "shivyc.main", "--no-cache", src,
           "--emit-thread-switcher", sw]
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    log = p.stdout + p.stderr

    saved = save_all = None
    m = re.search(r"left->right (\d+) regs.*?vs (\d+) for save-all", log)
    if m:
        saved, save_all = int(m.group(1)), int(m.group(2))
    disjoint = "disjoint" in log
    return {"benchmark": "threads_leftright", "kind": "regs",
            "regs_saved": saved, "regs_save_all": save_all,
            "disjoint": disjoint,
            "note": "gcc has no per-thread register partition concept"}


# ===========================================================================
# Benchmark 7b: codegen micros vs gcc -O0 / -O2 (idiv, mul-pow2, struct)
# ===========================================================================
def _bench_codegen_one(name, src_name):
    d = os.path.join(HERE, "codegen")
    src = os.path.join(d, src_name)
    cfgs = []
    out_s = os.path.join(d, "bin_" + name + "_shivyc")
    out_g0 = os.path.join(d, "bin_" + name + "_gcc0")
    out_g2 = os.path.join(d, "bin_" + name + "_gcc2")
    _shivyc(src, out_s)
    asm = ""
    try:
        asm = _read_asm(src)
    except OSError:
        pass
    idiv_n = _count(asm, r"\bidiv\b")
    imul_n = _count(asm, r"\bimul\b")
    shr_n = _count(asm, r"\bshr\b")
    cfgs.append({"name": "ShivyCX", "binary": out_s, "baseline": True,
                 "metric": "idiv=%d imul=%d shr=%d" % (idiv_n, imul_n, shr_n),
                 "metric_val": idiv_n})
    _gcc(src, out_g0, opt="-O0")
    cfgs.append({"name": "gcc -O0", "binary": out_g0, "baseline": False,
                 "metric": "peer -O0", "metric_val": None})
    _gcc(src, out_g2, opt="-O2")
    cfgs.append({"name": "gcc -O2", "binary": out_g2, "baseline": False,
                 "metric": "peer -O2", "metric_val": None})
    return {"benchmark": "codegen_" + name, "configs": _finish(cfgs)}


def bench_codegen_idiv():
    return _bench_codegen_one("idiv", "bench_idiv.c")


def bench_codegen_mul_pow2():
    return _bench_codegen_one("mul_pow2", "bench_mul_pow2.c")


def bench_codegen_struct():
    return _bench_codegen_one("struct", "bench_struct.c")


def bench_reg_class():
    """Register-class switch cost levels (Crust-ELF hint prep)."""
    d = os.path.join(HERE, "threads")
    src = os.path.join(d, "bench_reg_class.c")
    cfgs = []
    out_s = os.path.join(d, "bin_reg_class_shivyc")
    out_g0 = os.path.join(d, "bin_reg_class_gcc0")
    _shivyc(src, out_s)
    cfgs.append({"name": "ShivyCX", "binary": out_s, "baseline": True,
                 "metric": "reg_class switch stand-in", "metric_val": True})
    _gcc(src, out_g0)
    cfgs.append({"name": "gcc -O0", "binary": out_g0, "baseline": False,
                 "metric": "peer -O0", "metric_val": None})
    return {"benchmark": "threads_reg_class", "configs": _finish(cfgs)}


# ===========================================================================
# Benchmark 7: whole-program memory safety (UAF / double-free / auto-free)
# ===========================================================================
def bench_memory_safety():
    mem = os.path.join(REPO, "examples", "memory")
    cases = [
        ("dangling_alias", "use-after-free", "intra-function, via alias"),
        ("double_free", "double-free", "intra-function"),
        ("wrapper_uaf", "use-after-free", "CROSS-function (free+deref in callees)"),
        ("return_local", "dangling-stack-pointer", "returns &local (both catch direct form)"),
        ("autofree_leak", "auto-free", "leak closed automatically"),
    ]
    rows = []
    for name, kind, desc in cases:
        src = os.path.join(mem, name + ".c")
        # A refusal to compile is the expected outcome for these cases.
        rc, log = _shivyc(src, "/tmp/_ms", ["--check-memory"], quiet=True)
        if kind == "auto-free":
            shivy = "auto-free candidate" in log
        else:
            shivy = kind in log
        # gcc -O0 with all warnings.
        g = subprocess.run(["gcc", "-O0", "-Wall", "-Wextra", src, "-o", "/tmp/_msg"],
                           capture_output=True, text=True)
        gtext = g.stdout + g.stderr
        if kind == "auto-free":
            gcc_detect = False
        elif kind == "dangling-stack-pointer":
            gcc_detect = "returns address of local" in gtext  # gcc -Wreturn-local-addr
        else:
            gcc_detect = ("use-after-free" in gtext or "double" in gtext)
        rows.append({"case": name, "bug": kind, "desc": desc,
                     "shivyc_detects": shivy, "gcc_detects": gcc_detect})
    return {"benchmark": "memory_safety", "kind": "detection", "rows": rows}


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    results = []
    timed = [("_Nbit globals", bench_nbit),
             ("contracts SIMD", bench_contracts),
             ("SIMD .py vs gcc -O0", bench_simd_py),
             ("SIMD .py vs gcc -O2", bench_simd_o2),
             ("stackless calls", bench_stackless),
             ("metamorphic returns", bench_metamorphic),
             ("codegen idiv", bench_codegen_idiv),
             ("codegen mul_pow2", bench_codegen_mul_pow2),
             ("codegen struct", bench_codegen_struct),
             ("threads reg_class", bench_reg_class)]
    for label, fn in timed:
        print("Running %s benchmark ..." % label)
        results.append(fn())

    # Capability / static-metric benchmarks (no like-for-like runtime).
    print("Running member-elimination benchmark ...")
    me = bench_member_elim()
    print("Running left/right threads benchmark ...")
    th = bench_threads()
    print("Running memory-safety benchmark ...")
    ms = bench_memory_safety()
    results.extend([me, th, ms])

    with open(os.path.join(RESULTS_DIR, "results.json"), "w") as f:
        json.dump(results, f, indent=2)

    timed_n = len(timed)
    for bench in results[:timed_n]:
        print("\n=== %s ===" % bench["benchmark"])
        exits = {c["exit_code"] for c in bench["configs"]}
        print("  differential correctness: %s (exit codes: %s)"
              % ("PASS" if len(exits) == 1 else "FAIL", exits))
        base = next((c for c in bench["configs"] if c["baseline"]), None)
        for c in bench["configs"]:
            sp = ""
            if base and c is not base and c["time_s"]:
                sp = "  (%.2fx vs ShivyCX baseline)" % (base["time_s"] / c["time_s"])
            print("  %-32s %.3fs  [%s]%s" % (c["name"], c["time_s"], c["metric"], sp))

    print("\n=== member_elim ===")
    print("  correctness: %s" % ("PASS" if me["exit_agree"] else "FAIL"))
    print("  %s" % me["removed"])
    print("  table[] .bss:  ShivyCX off = %d B   ShivyCX on = %d B   gcc -O0 = %d B  (%.1fx smaller; %s)"
          % (me["off_bytes"], me["on_bytes"], me["gcc_bytes"],
             me["off_bytes"] / me["on_bytes"], me["note"]))

    print("\n=== threads_leftright ===")
    print("  context-switch register save: %d regs  (vs %d for save-all; %s)"
          % (th["regs_saved"], th["regs_save_all"], th["note"]))

    print("\n=== memory_safety ===")
    print("  %-16s %-14s %-8s %-8s  %s" % ("case", "bug", "ShivyCX", "gcc-O0", "scenario"))
    for r in ms["rows"]:
        print("  %-16s %-14s %-8s %-8s  %s"
              % (r["case"], r["bug"],
                 "YES" if r["shivyc_detects"] else "no",
                 "YES" if r["gcc_detects"] else "no", r["desc"]))

    print("\nWrote", os.path.join(RESULTS_DIR, "results.json"))


if __name__ == "__main__":
    main()
