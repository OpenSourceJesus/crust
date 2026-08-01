#!/usr/bin/env python3
"""Cross-runtime benchmark harness for rpython (.py) programs.

Every benchmark in ``benchmarks/rpython/`` is a *pure-Python* program that is
runnable three ways and compilable two more, giving four execution backends that
all produce the same result:

  * **cpython**  -- run the .py directly under CPython.
  * **pypy3**    -- run the .py directly under PyPy3 (omitted if pypy3 absent).
  * **gcc**      -- transpile to C with tools/py2c.py, compile with ``gcc -O2``.
  * **selfhost** -- transpile the same C, compile with the *self-hosted* ShivyCX
                    compiler (the native ``shivyc_native`` binary built from the
                    compiler's own sources).

For every backend the harness records:
  * **runtime**     -- best-of-N wall-clock seconds of the program itself,
  * **memory**      -- peak resident set size (RSS) of the process, exactly, via
                       ``os.wait4`` rusage of the specific child, and
  * **compile**     -- for the two compiled backends, the wall-clock seconds the
                       C compiler (gcc / shivyc_native) took on the py2c output
                       (the py2c transpile time is shared and recorded once).

All four backends must agree on the program's exit code (differential
correctness); a disagreement is flagged.

Results -> benchmarks/results/rpython_results.json.
"""
import json
import os
import resource
import signal
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RPY_DIR = os.path.join(HERE, "rpython")
RESULTS_DIR = os.path.join(HERE, "results")
TOOLS = os.path.join(REPO, "tools")

# Make `tools/` importable for the interpreted backends (CPython, PyPy3) so a
# benchmark can `import rpy` for the typed-json oracle. The translator never
# compiles `import rpy` -- it only pattern-matches rpy.json.generate_decoder --
# so this affects only the interpreted runs, keeping benchmark files clean.
os.environ["PYTHONPATH"] = (
    TOOLS + os.pathsep + os.environ.get("PYTHONPATH", "")).rstrip(os.pathsep)

REPS = 5                      # best-of-N for runtime
# No step should ever hang the whole report. A wedged compiler or a benchmark
# that fails to terminate is killed and reported, not waited on forever.
COMPILE_TIMEOUT = int(os.environ.get("BENCH_COMPILE_TIMEOUT", "300"))
RUN_TIMEOUT = int(os.environ.get("BENCH_RUN_TIMEOUT", "300"))
DEVNULL = open(os.devnull, "wb")

# A tiny C helper measures a child's exact peak RSS. We must NOT fork from this
# (heavy) Python harness to measure memory: a forked child inherits the parent's
# resident pages (copy-on-write) before exec, so its ru_maxrss is floored by the
# harness's own ~12 MB footprint. A ~1.5 MB C process forking the target gives
# the target's real peak instead.
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


# --------------------------------------------------------------------------
# measurement primitives
# --------------------------------------------------------------------------
def _run_measured(argv, mem_probe, reps=REPS):
    """Return (best_wall_seconds, peak_rss_kb, exit_code).

    Runtime is best-of-N timed directly (subprocess overhead is constant);
    memory is the child's exact peak RSS via the minimal-footprint C probe.
    """
    best = float("inf")
    exit_code = None
    for _ in range(reps):
        t0 = time.perf_counter()
        try:
            p = subprocess.run(argv, stdout=DEVNULL, stderr=DEVNULL,
                               timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            print("  TIMEOUT after %ds: %s" % (RUN_TIMEOUT, " ".join(argv)))
            return (time.perf_counter() - t0), 0, None
        best = min(best, time.perf_counter() - t0)
        exit_code = p.returncode
        if exit_code < 0:
            print("  died on signal %d (%s): %s"
                  % (-exit_code, signal.Signals(-exit_code).name,
                     " ".join(argv)))
            break
    try:
        mp = subprocess.run([mem_probe] + argv, stdout=DEVNULL,
                            stderr=subprocess.PIPE, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return best, 0, exit_code
    try:
        rss_kb = int(mp.stderr.strip().splitlines()[-1])
    except (ValueError, IndexError):
        rss_kb = 0
    return best, rss_kb, exit_code


def _describe(p, out_binary):
    """Human-readable reason a compile failed. A process killed by a signal
    writes nothing to the pipe, so the raw stdout is empty and the caller would
    otherwise print `FAILED:` followed by a blank line -- which is exactly what
    a SIGSEGV in the compiler looked like."""
    log = p.stdout.decode("utf-8", "replace") if p.stdout else ""
    if p.returncode < 0:
        signame = signal.Signals(-p.returncode).name
        note = "compiler died on signal %d (%s)" % (-p.returncode, signame)
        return (note + "\n" + log) if log.strip() else note
    if p.returncode != 0:
        return log if log.strip() else ("exited %d with no output"
                                        % p.returncode)
    if not os.path.exists(out_binary):
        return log if log.strip() else ("exited 0 but produced no %s"
                                        % os.path.basename(out_binary))
    return log


def _compile_timed(argv, out_binary, timeout=COMPILE_TIMEOUT):
    """Run a compiler command, returning (seconds, ok). Best-of-3 so a cold
    cache / scheduler hiccup does not dominate. A hung compiler is killed
    rather than hanging the whole run."""
    best = float("inf")
    ok = False
    for _ in range(3):
        if os.path.exists(out_binary):
            os.remove(out_binary)
        t0 = time.perf_counter()
        try:
            p = subprocess.run(argv, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            return (time.perf_counter() - t0), False, \
                "compiler timed out after %ds: %s" % (timeout, " ".join(argv))
        dt = time.perf_counter() - t0
        best = min(best, dt)
        ok = (p.returncode == 0 and os.path.exists(out_binary))
        if not ok:
            return best, False, _describe(p, out_binary)
    return best, ok, ""


# --------------------------------------------------------------------------
# the self-hosted native compiler
# --------------------------------------------------------------------------
def ensure_native(build_dir=None):
    """Return (native_binary, include_dir) for the self-hosted compiler,
    building it from the compiler's own sources if needed. A prebuilt one can be
    supplied via $SHIVYC_NATIVE (its includes via $SHIVYC_NATIVE_INC)."""
    env = os.environ.get("SHIVYC_NATIVE")
    if env and os.path.exists(env):
        inc = os.environ.get("SHIVYC_NATIVE_INC",
                              os.path.join(os.path.dirname(env), "include"))
        return env, inc
    build_dir = build_dir or os.path.join(REPO, "build_native_bench")
    native = os.path.join(build_dir, "shivyc_native")
    inc = os.path.join(build_dir, "include")
    if not os.path.exists(native):
        print("Building self-hosted native compiler (one-time, ~2-3 min) ...")
        try:
            p = subprocess.run(
                [sys.executable, os.path.join(TOOLS, "selfhost.py"),
                 "compiler", "--build-dir", build_dir],
                cwd=REPO, timeout=COMPILE_TIMEOUT * 4)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "building shivyc_native timed out; a stale partial build in "
                "%s will be reused on the next run because only its existence "
                "is checked -- remove that directory and retry" % build_dir)
        if p.returncode != 0 or not os.path.exists(native):
            raise RuntimeError("could not build shivyc_native")
    return native, inc


# --------------------------------------------------------------------------
# py2c transpile + the two compiled backends
# --------------------------------------------------------------------------
def transpile(py_file, out_dir):
    """py2c-transpile py_file (rpython) into out_dir, emitting the runtime once.
    Returns (module_c_path, transpile_seconds)."""
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    import py2c
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.perf_counter()
    py2c.write_runtime(out_dir)
    cpath, err = py2c.transpile_file(py_file, out_dir)
    dt = time.perf_counter() - t0
    if err or not cpath:
        raise RuntimeError("py2c failed on %s: %s" % (py_file, err))
    return cpath, dt


def _merge_single_tu(out_dir, module_c):
    """Concatenate the runtime and the module into one translation unit (the
    self-hosted compiler compiles+links a single .c at a time)."""
    merged = os.path.join(out_dir, "merged.c")
    with open(merged, "w") as o:
        o.write('#include "shivyc_rt.h"\n')
        for f in ("shivyc_rt.c", os.path.basename(module_c)):
            with open(os.path.join(out_dir, f)) as fh:
                for ln in fh:
                    if ln.strip() != '#include "shivyc_rt.h"':
                        o.write(ln)
    return merged


def compile_gcc(out_dir, module_c, opt="-O2"):
    out = os.path.join(out_dir, "bin_gcc")
    argv = ["gcc", opt, "-I", out_dir,
            module_c, os.path.join(out_dir, "shivyc_rt.c"), "-o", out, "-lm"]
    secs, ok, log = _compile_timed(argv, out)
    return (out if ok else None), secs, log


def compile_selfhost(out_dir, module_c, native, inc):
    merged = _merge_single_tu(out_dir, module_c)
    out = os.path.join(out_dir, "bin_selfhost")
    argv = [native, "-I", out_dir, "-I", inc, merged, "-o", out]
    secs, ok, log = _compile_timed(argv, out)
    return (out if ok else None), secs, log


# --------------------------------------------------------------------------
# one benchmark
# --------------------------------------------------------------------------
def run_one(py_file, native, inc, mem_probe, run_args=None):
    run_args = run_args or []
    name = os.path.splitext(os.path.basename(py_file))[0]
    out_dir = os.path.join("/tmp", "rpybench_" + name)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)

    module_c, transpile_s = transpile(py_file, out_dir)
    gcc_bin, gcc_cs, gcc_log = compile_gcc(out_dir, module_c)
    self_bin, self_cs, self_log = compile_selfhost(out_dir, module_c, native, inc)

    backends = {}

    def measure(label, argv, compile_s=None):
        rt, rss, ec = _run_measured(argv, mem_probe)
        backends[label] = {"runtime_s": rt, "rss_kb": rss, "exit": ec,
                           "compile_s": compile_s}

    measure("cpython", [sys.executable, py_file] + run_args)
    if shutil.which("pypy3"):
        measure("pypy3", ["pypy3", py_file] + run_args)
    if gcc_bin:
        measure("gcc", [gcc_bin] + run_args, compile_s=gcc_cs)
    else:
        print("  gcc compile FAILED:\n", gcc_log[:400])
    if self_bin:
        measure("selfhost", [self_bin] + run_args, compile_s=self_cs)
    else:
        print("  selfhost compile FAILED:\n", self_log[:400])

    exits = {b["exit"] for b in backends.values()}
    return {"benchmark": name, "run_args": run_args,
            "transpile_s": transpile_s,
            "exit_agree": len(exits) == 1, "exits": sorted(exits),
            "backends": backends}


# benchmark name -> run-time arguments (work size). Kept here so a single knob
# tunes each program without editing the .py.
BENCH_ARGS = {
    "fib": ["32"],
    "mandelbrot": ["140"],
    "sieve": ["4000000"],
    "matmul": ["95"],
    "binary_trees": ["15"],
    "stats": ["6000"],
    "svgplot": ["40000"],
    "json_decode": ["200000"],
    # Wired orphans + OS-shaped CrustOS micros
    "ack": ["3", "6"],
    "nbody": ["20000"],
    "scheme_route": ["200000"],
    "frame_alloc": ["20000"],
    "sched_ticks": ["500000"],
}

# `--quick`: the same benchmarks at a fraction of the workload. Scaling the
# arguments rather than dropping benchmarks keeps every backend comparison
# intact -- what changes is the precision of the numbers, not what is covered.
# Ratios between backends stay meaningful; absolute times are not comparable
# with a full run, so the results are marked.
QUICK_DIVISOR = 8
QUICK_ARGS = {
    "fib": ["24"],              # exponential: divide the exponent, not the arg
    "ack": ["3", "3"],
    "binary_trees": ["11"],
}


def quick_args(name, args):
    """Smaller workload for `name`, or None to leave it unchanged."""
    if name in QUICK_ARGS:
        return QUICK_ARGS[name]
    out = []
    for a in args:
        try:
            n = int(a)
        except ValueError:
            return None
        out.append(str(max(1, n // QUICK_DIVISOR)))
    return out or None


def discover():
    if not os.path.isdir(RPY_DIR):
        return []
    return sorted(f for f in os.listdir(RPY_DIR) if f.endswith(".py"))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    quick = "--quick" in argv or "-q" in argv
    if quick:
        print("quick: reduced workloads; times are not comparable with a "
              "full run\n")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    native, inc = ensure_native()
    mem_probe = build_mem_probe()
    results = []
    for fn in discover():
        name = fn[:-3]
        py_file = os.path.join(RPY_DIR, fn)
        args = BENCH_ARGS.get(name, [])
        if quick:
            args = quick_args(name, args) or args
        print("Running rpython benchmark: %s %s" % (name, " ".join(args)))
        try:
            r = run_one(py_file, native, inc, mem_probe, args)
        except Exception as e:
            print("  ERROR:", e)
            continue
        results.append(r)
        agree = "PASS" if r["exit_agree"] else "FAIL %s" % r["exits"]
        print("  correctness: %s" % agree)
        for label in ("cpython", "pypy3", "gcc", "selfhost"):
            b = r["backends"].get(label)
            if not b:
                continue
            cs = ("  compile %.3fs" % b["compile_s"]) if b["compile_s"] else ""
            print("    %-9s runtime %.4fs  rss %6d KB%s"
                  % (label, b["runtime_s"], b["rss_kb"], cs))

    out = os.path.join(RESULTS_DIR, "rpython_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print("\nWrote", out)
    return results


if __name__ == "__main__":
    main()
