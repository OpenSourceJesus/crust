#!/usr/bin/env python3
"""Cross-runtime benchmark harness for the **minipy** interpreter.

minipy is a tiny register-bytecode interpreter (``tools/minipy``) written in the
py2c-compilable RPython subset. A Python program is compiled to flattened
bytecode (JSON) by ``tools/minipy/compiler.py`` and then executed by the
interpreter ``tools/minipy/interp.py`` -- which is itself transpiled to C by
``tools/py2c.py`` and compiled with gcc, producing a standalone native binary.

Every benchmark in ``benchmarks/minipy/`` is a self-contained pure-Python program
(no imports / argv -- minipy runs top-level module code) that prints a result.
It is executed on each available backend:

  * **cpython**  -- run the .py directly under CPython.
  * **pypy3**    -- run the .py directly under PyPy3 (skipped if pypy3 absent).
  * **minipy**   -- compile to bytecode, run on the native interp binary.

Differential correctness: all backends must print identical stdout. For every
backend the harness records best-of-N wall-clock runtime and peak RSS (via a
minimal-footprint C probe so the measurement does not inherit this harness's own
resident pages). For minipy it also records the one-time interp build time and
the per-benchmark bytecode-compile time.

Results -> benchmarks/results/minipy_results.json.
"""
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BENCH_DIR = os.path.join(HERE, "minipy")
RESULTS_DIR = os.path.join(HERE, "results")
TOOLS = os.path.join(REPO, "tools")

REPS = 3                      # best-of-N for runtime
RUN_TIMEOUT = 60              # per-process wall-clock cap (seconds)
DEVNULL = open(os.devnull, "wb")

# Minimal-footprint child that measures the target's exact peak RSS. Forking from
# this heavy Python harness would floor ru_maxrss at our own footprint; a ~1.5 MB
# C process forking the target reports the target's real peak.
_MEM_PROBE_SRC = r"""
#include <stdio.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/wait.h>
#include <sys/resource.h>
int main(int argc, char** argv) {
    if (argc < 2) return 2;
    pid_t pid = fork();
    if (pid == 0) {
        int dn = open("/dev/null", O_WRONLY);
        if (dn >= 0) { dup2(dn, 1); dup2(dn, 2); }
        execvp(argv[1], &argv[1]);
        _exit(127);
    }
    int status; struct rusage ru;
    wait4(pid, &status, 0, &ru);
    fprintf(stderr, "%ld\n", ru.ru_maxrss);   /* KB on Linux */
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}
"""


def build_mem_probe():
    path = os.path.join("/tmp", "shivyc_mem_probe")
    if not os.path.exists(path):
        src = path + ".c"
        with open(src, "w") as f:
            f.write(_MEM_PROBE_SRC)
        subprocess.run(["gcc", "-O2", src, "-o", path], check=True)
    return path


def _capture(argv):
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=RUN_TIMEOUT)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -9, "<timeout>"


def _run_measured(argv, mem_probe, reps=REPS):
    """Return (best_wall_seconds, peak_rss_kb, exit_code, stdout)."""
    rc, out = _capture(argv)               # one capture run for correctness
    if rc == -9:
        return float("inf"), 0, rc, out
    best = float("inf")
    for _ in range(reps):
        t0 = time.perf_counter()
        subprocess.run(argv, stdout=DEVNULL, stderr=DEVNULL,
                       timeout=RUN_TIMEOUT)
        best = min(best, time.perf_counter() - t0)
    mp = subprocess.run([mem_probe] + argv,
                        stdout=DEVNULL, stderr=subprocess.PIPE)
    try:
        rss_kb = int(mp.stderr.strip().splitlines()[-1])
    except (ValueError, IndexError):
        rss_kb = 0
    return best, rss_kb, rc, out


# --------------------------------------------------------------------------
# building the native minipy interpreter (once)
# --------------------------------------------------------------------------
def build_interp():
    """Transpile tools/minipy/interp.py to C and gcc it to a native binary.
    Returns (interp_binary, build_seconds)."""
    out_dir = os.path.join("/tmp", "minipy_bench_interp")
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    import py2c
    t0 = time.perf_counter()
    py2c.write_runtime(out_dir)
    cpath, err = py2c.transpile_file(
        os.path.join(TOOLS, "minipy", "interp.py"), out_dir)
    if err or not cpath:
        raise RuntimeError("py2c failed on interp.py: %s" % err)
    binary = os.path.join(out_dir, "interp")
    # The interpreter's ctypes FFI builtins lower to calls into the mb_ffi.c
    # shim (dlopen/dlsym plus the indirect-call thunks). `tools/rpy.py` copies
    # it in before globbing for the same reason; this path did not, so the
    # link failed on `mb_dlsym`, `mb_call1i` and friends.
    mbffi = os.path.join(TOOLS, "rpy_lib", "mb_ffi.c")
    if os.path.isfile(mbffi):
        shutil.copy(mbffi, os.path.join(out_dir, "mb_ffi.c"))
    csrcs = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
             if f.endswith(".c")]
    p = subprocess.run(["gcc", "-std=c99", "-O2", "-I", out_dir]
                       + csrcs + ["-o", binary, "-lm"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0 or not os.path.exists(binary):
        raise RuntimeError("gcc failed building interp:\n"
                           + p.stdout.decode("utf-8", "replace")[:800])
    return binary, time.perf_counter() - t0


def compile_bytecode(py_file):
    """Compile a benchmark to minipy bytecode JSON. Returns (json_path, secs)."""
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    from minipy import compiler as C
    name = os.path.splitext(os.path.basename(py_file))[0]
    out = os.path.join("/tmp", "minipy_bench_" + name + ".bc.json")
    t0 = time.perf_counter()
    prog = C.compile_file(py_file)
    dt = time.perf_counter() - t0
    with open(out, "w") as f:
        json.dump(prog, f)
    return out, dt


# --------------------------------------------------------------------------
# one benchmark
# --------------------------------------------------------------------------
def run_one(py_file, interp_bin, mem_probe):
    name = os.path.splitext(os.path.basename(py_file))[0]
    backends = {}

    def measure(label, argv, compile_s=None):
        rt, rss, ec, out = _run_measured(argv, mem_probe)
        backends[label] = {"runtime_s": rt, "rss_kb": rss, "exit": ec,
                           "stdout": out.strip(), "compile_s": compile_s}

    measure("cpython", [sys.executable, py_file])
    if shutil.which("pypy3"):
        measure("pypy3", ["pypy3", py_file])

    bc_path, compile_s = compile_bytecode(py_file)
    measure("minipy", [interp_bin, bc_path], compile_s=compile_s)

    outs = {b["stdout"] for b in backends.values()}
    return {"benchmark": name,
            "output_agree": len(outs) == 1,
            "outputs": sorted(outs),
            "backends": backends}


def discover():
    if not os.path.isdir(BENCH_DIR):
        return []
    return sorted(f for f in os.listdir(BENCH_DIR) if f.endswith(".py"))


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    mem_probe = build_mem_probe()
    print("Building native minipy interpreter (py2c -> gcc) ...")
    interp_bin, build_s = build_interp()
    print("  interp built in %.2fs -> %s" % (build_s, interp_bin))

    results = []
    for fn in discover():
        name = fn[:-3]
        py_file = os.path.join(BENCH_DIR, fn)
        print("Running minipy benchmark: %s" % name)
        try:
            r = run_one(py_file, interp_bin, mem_probe)
        except Exception as e:
            print("  ERROR:", e)
            continue
        r["interp_build_s"] = build_s
        results.append(r)
        agree = "PASS" if r["output_agree"] else "FAIL %r" % r["outputs"]
        print("  correctness: %s" % agree)
        base = r["backends"].get("cpython", {}).get("runtime_s")
        for label in ("cpython", "pypy3", "minipy"):
            b = r["backends"].get(label)
            if not b:
                continue
            rel = ""
            if base and b["runtime_s"]:
                rel = "  (%.1fx cpython)" % (b["runtime_s"] / base)
            cs = ("  compile %.3fs" % b["compile_s"]) if b["compile_s"] else ""
            print("    %-8s runtime %.4fs  rss %6d KB%s%s"
                  % (label, b["runtime_s"], b["rss_kb"], cs, rel))

    out = os.path.join(RESULTS_DIR, "minipy_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print("\nWrote", out)
    return results


if __name__ == "__main__":
    main()
