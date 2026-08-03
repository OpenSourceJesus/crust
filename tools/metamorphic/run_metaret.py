#!/usr/bin/env python3
"""Build and run the metamorphic-return microbenchmarks, reporting the median
cyc/call over several runs. Everything is produced through the self-hosted
path: rasm assembles, rlink links at a low base (0x1000), no external as/ld.

    python3 run_metaret.py            # both benchmarks, 7 runs each
    python3 run_metaret.py --runs 11
"""
import os, re, subprocess, sys, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
LINE = re.compile(r"^(\S.*?):\s+\d+\s+cyc\s+([\d.]+)", re.M)


def build(gen, asm, out, base=0x1000):
    subprocess.run([sys.executable, os.path.join(HERE, gen), asm],
                   cwd=HERE, check=True, stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable, os.path.join(HERE, "rbuild.py"),
                    asm, out, hex(base)], cwd=HERE, check=True,
                   stdout=subprocess.DEVNULL)


def run_median(binary, runs):
    series = {}
    correct = True
    for _ in range(runs):
        out = subprocess.run([os.path.join(HERE, binary)], cwd=HERE,
                             capture_output=True, text=True).stdout
        if "MISMATCH" in out:
            correct = False
        for label, cpc in LINE.findall(out):
            series.setdefault(label.strip(), []).append(float(cpc))
    return [(lbl, statistics.median(v)) for lbl, v in series.items()], correct


def report(title, rows, correct):
    print("\n" + title)
    print("-" * len(title))
    base = min(c for _, c in rows)
    for lbl, c in rows:
        print("  %-40s %8.2f cyc/call   %5.1fx" % (lbl, c, c / base))
    print("  correctness: %s" % ("OK" if correct else "*** MISMATCH ***"))


def main():
    runs = 7
    if "--runs" in sys.argv:
        runs = int(sys.argv[sys.argv.index("--runs") + 1])

    build("gen_metaret_variants.py", "bench_metaret_variants.s",
          "bench_metaret_variants")
    rows, ok = run_median("bench_metaret_variants", runs)
    report("Benchmark 1: slot placement and write width (median of %d)" % runs,
           rows, ok)

    build("gen_trampoline.py", "bench_trampoline.s", "bench_trampoline")
    rows, ok = run_median("bench_trampoline", runs)
    report("Benchmark 2: trampoline return, where metamorphic wins (median of %d)"
           % runs, rows, ok)


if __name__ == "__main__":
    main()
