#!/usr/bin/env python3
"""Measure what the register partition buys on a preemptive context switch.

Builds the same bare-metal kernel twice, changing exactly one thing: which
switcher sits in the EL1h IRQ vector slot.

    partitioned   the ISR ShivyCX generated from the whole-program left/right
                  partition -- saves only the running thread's footprint
    save-all      baremetal64/switcher_full_arm64.S -- saves all 31 GP
                  registers, which is what a scheduler with no partition
                  information must do

Two things this measurement had to get right, both of which produced wrong
answers first:

**The baseline must be correct, not merely generic.** The obvious comparison is
`exc_common` in vectors_arm64.S, which saves twenty-two registers. But it never
saves x20-x28, and ShivyCX puts value homes there -- as a thread switcher it
would silently corrupt state. Comparing against it would have flattered the
partitioned version by nine registers it was never entitled to skip. The honest
baseline is all 31.

**qemu must run under -icount.** Without it, CNTPCT follows host wall time, so
two runs at different host loads are not comparable. Measured that way the
partitioned build looked 1.64x faster at 20 kHz -- and simultaneously reported
*more* cycles per switch, a contradiction that was the only clue the metric was
junk. Under `-icount shift=0` guest time advances one nanosecond per
instruction executed, runs are deterministic, and the numbers stop
contradicting each other.

Building two bare-metal images and emulating both takes several minutes, which
is longer than some harnesses allow for a single command, so the work is split
into phases that share a directory:

    python3 tools/bench_preempt.py --build   --workdir /tmp/bp
    python3 tools/bench_preempt.py --measure --workdir /tmp/bp

Run with neither flag to do both in one go, and `--sweep` to cover several tick
rates instead of one.
"""
import os
import re
import subprocess
import sys
import tempfile

class _KeepDir(object):
    """Stand-in for TemporaryDirectory that does not delete, so --build and
    --measure can run as separate commands over the same artifacts."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self.path

    def __exit__(self, *a):
        return False


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

BENCH = os.path.join(ROOT, "benchmarks", "threads", "kernel_preempt_bench.c")

DECL = """void worker_left(void); void worker_right(void);
int main()
assert worker_left in threads.left( core=0 )
assert worker_right in threads.right( core=0 )
{ worker_left(); worker_right(); return 0; }
"""


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, **kw)
    return p.returncode, p.stdout, p.stderr


def isr_instructions(path, label):
    """Instructions in one ISR body, from its label to its eret."""
    on = False
    n = 0
    op = re.compile(r"^(str|ldr|stp|ldp|mov|mrs|msr|adrp|add|sub|bl|b |eret"
                    r"|ret)")
    for ln in open(path):
        t = ln.strip()
        if t.startswith(label + ":"):
            on = True
            continue
        if on and op.match(t):
            n += 1
        if on and t == "eret":
            break
    return n


def build(switcher, hz, switches, out, defines_extra=None):
    import baremetal_arm64 as bm
    d = os.path.dirname(out)
    cmd = [sys.executable, os.path.join(HERE, "baremetal_arm64.py"), BENCH,
           "--extra-asm", "vectors_preempt_arm64.S",
           "--extra-asm", switcher,
           "-DTICK_HZ=%d" % hz, "-DTARGET_SWITCHES=%d" % switches,
           "-o", out]
    rc, o, e = run(cmd)
    return rc == 0 and os.path.exists(out), (o + e)


def measure(elf, timeout=200):
    cmd = ["qemu-system-aarch64", "-M", "virt", "-cpu", "cortex-a57",
           "-icount", "shift=0", "-nographic", "-nodefaults",
           "-serial", "mon:stdio", "-kernel", elf]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
    m = re.search(r"RESULT switches=(\d+) work=(\d+) left=(\d+) right=(\d+)"
                  r" cycles=(\d+) corrupt=(\d+)", out.replace("\r", ""))
    if not m:
        return None
    return {"switches": int(m.group(1)), "work": int(m.group(2)),
            "cycles": int(m.group(5)), "corrupt": int(m.group(6))}


def parse_args():
    argv = sys.argv[1:]
    workdir = None
    if "--workdir" in argv:
        workdir = argv[argv.index("--workdir") + 1]
    do_build = "--build" in argv
    do_measure = "--measure" in argv
    if not do_build and not do_measure:
        do_build = do_measure = True
    return workdir, do_build, do_measure, "--sweep" in argv


def main():
    workdir, do_build, do_measure, sweep = parse_args()
    rates = [1000, 20000, 1000000, 8000000] if sweep else [8000000]
    if (do_build != do_measure) and not workdir:
        print("--build and --measure need --workdir to share artifacts")
        return 2

    if subprocess.run(["which", "qemu-system-aarch64"],
                      capture_output=True).returncode != 0:
        print("SKIP: qemu-system-aarch64 not installed")
        return 0

    if workdir:
        os.makedirs(workdir, exist_ok=True)
        ctx = _KeepDir(workdir)
    else:
        ctx = tempfile.TemporaryDirectory()
    with ctx as d:
        decl = os.path.join(d, "decl.c")
        with open(decl, "w") as f:
            f.write(DECL)
        sw = os.path.join(d, "sw.s")
        pre = sw[:-2] + ".preempt.s"
        if do_build:
            rc, o, e = run([sys.executable, "-m", "shivyc.main", BENCH, decl,
                            "--emit-thread-switcher", sw, "--target", "arm64"])
        else:
            rc, o, e = 0, "", ""
        if rc != 0 or not os.path.exists(pre):
            print("FAIL: could not generate the partitioned switcher")
            print((o + e)[-500:])
            return 1

        full = os.path.join(ROOT, "baremetal64", "switcher_full_arm64.S")
        n_part = isr_instructions(pre, "timer_isr_left")
        n_full = isr_instructions(full, "timer_dispatch_full")

        # The exact, emulator-independent part of the result.
        print("ISR size (instructions between entry and eret)")
        print("  partitioned : %3d" % n_part)
        print("  save-all    : %3d" % n_full)
        print("  saved       : %3d  (%.0f%% smaller)"
              % (n_full - n_part, 100.0 * (n_full - n_part) / n_full))
        print()
        if do_measure:
            print("Throughput, qemu -icount shift=0 (1 instruction = 1 ns)")
            print("  %9s  %14s  %14s  %8s" %
                  ("tick rate", "part cyc/switch", "full cyc/switch",
                   "delta"))

        rc_out = 0
        for hz in rates:
            row = []
            for name, switcher in (("part", pre), ("full", full)):
                elf = os.path.join(d, "%s_%d.elf" % (name, hz))
                if do_build:
                    ok, log = build(switcher, hz, 400, elf)
                    if not ok:
                        print("  build failed for %s at %d Hz" % (name, hz))
                        return 1
                if not do_measure:
                    continue
                if not os.path.exists(elf):
                    print("  %s_%d.elf missing -- run --build first" %
                          (name, hz))
                    return 1
                r = measure(elf)
                if r is None:
                    print("  %9d  (no result -- raise the timeout)" % hz)
                    row = None
                    break
                if r["corrupt"]:
                    print("  %9d  %s CORRUPTED (%d) -- result discarded"
                          % (hz, name, r["corrupt"]))
                    rc_out = 1
                row.append(r)
            if not row:
                continue
            p_cs = float(row[0]["cycles"]) / max(row[0]["switches"], 1)
            f_cs = float(row[1]["cycles"]) / max(row[1]["switches"], 1)
            delta = 100.0 * (f_cs - p_cs) / f_cs
            print("  %9d  %14.2f  %14.2f  %7.1f%%" % (hz, p_cs, f_cs, delta))

        if not do_measure:
            print("built %d image pair(s) in %s" % (len(rates), d))
            print("now: python3 tools/bench_preempt.py --measure --workdir %s"
                  % d)
            return 0

    print()
    print("A switch costs the same absolute number of instructions at every")
    print("rate; what changes is how much work sits between switches. The")
    print("saving only shows up once switches are frequent enough to matter.")
    return rc_out


if __name__ == "__main__":
    sys.exit(main())
