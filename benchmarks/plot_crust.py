#!/usr/bin/env python3
"""Render `results/crust_results.json` to runtime and binary-size figures.

Two panels per figure rather than one combined score: the benchmarks measure
different parts of the lowering and a single number would hide which part
moved.

Usage: plot_crust.py [out_dir]
"""

import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results", "crust_results.json")

ORDER = ["ShivyCX", "gcc -O0", "gcc -O2", "gcc -O3"]
COLORS = {"ShivyCX": "#DD8452", "gcc -O0": "#999999",
          "gcc -O2": "#6A9F58", "gcc -O3": "#4C72B0"}


def _series(benches, key):
    """`(labels, {config: [value per benchmark]})` for one metric."""
    labels = [b["benchmark"] for b in benches]
    out = {}
    for cfg in ORDER:
        vals = []
        for b in benches:
            hit = [c for c in b["configs"] if c["name"] == cfg and c.get("ok")]
            vals.append(hit[0][key] if hit else 0.0)
        out[cfg] = vals
    return labels, out


def _grouped(ax, labels, series, ylabel, title):
    n = len(labels)
    width = 0.8 / len(ORDER)
    xs = range(n)
    for i, cfg in enumerate(ORDER):
        off = [x + i * width - 0.4 + width / 2 for x in xs]
        ax.bar(off, series[cfg], width, label=cfg, color=COLORS[cfg])
    ax.set_xticks(list(xs))
    ax.set_xticklabels(labels, rotation=15, ha="right", fontsize=9)
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=11)
    ax.grid(axis="y", alpha=0.3)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "results")
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.exists(RESULTS):
        print("no crust_results.json; run run_crust_benchmarks.py first")
        return 1
    with open(RESULTS) as f:
        benches = json.load(f)
    if not benches:
        print("crust_results.json is empty")
        return 1

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 8))
    labels, times = _series(benches, "time_s")
    _grouped(a1, labels, times, "seconds (best of 3)",
             "Crust benchmarks: runtime, same generated C")
    labels, sizes = _series(benches, "size_bytes")
    sizes = {k: [v / 1024.0 for v in vs] for k, vs in sizes.items()}
    _grouped(a2, labels, sizes, "binary size (KiB)",
             "Binary size")
    a1.legend(ncol=4, fontsize=9, loc="upper right")
    fig.suptitle("Crust (C+Rust) versus gcc on identical C input", fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(out_dir, "crust_bench." + ext), dpi=130)
    print("wrote %s/crust_bench.{png,pdf}" % out_dir)

    # A LaTeX fragment describing each benchmark, so the report explains what
    # every bar means rather than showing an unlabelled comparison.
    tex = ["\\begin{description}"]
    for b in benches:
        desc = b.get("description") or "(no description)"
        tex.append("\\item[\\texttt{%s}] %s"
                   % (b["benchmark"].replace("_", "\\_"), desc))
    tex.append("\\end{description}")
    body = os.path.join(out_dir, "crust_bench_body.tex")
    with open(body, "w") as f:
        f.write("\n".join(tex) + "\n")
    print("wrote %s" % body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
