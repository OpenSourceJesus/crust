#!/usr/bin/env python3
"""Render results/metamorphic_results.json to a two-panel figure and a LaTeX
body fragment for the benchmark report.

Left panel: the cost of each way to lower a return, on a log axis (per-call
patching is two orders of magnitude off the others, so a linear axis would
hide everything else). Right panel: call/ret vs hoisted metamorphic as the
call chain deepens -- the return-stack-buffer crossover.

Usage: plot_metamorphic.py [out_dir]
"""
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results", "metamorphic_results.json")

GROUP_COLOR = {"reference": "#4C72B0", "metamorphic": "#DD8452",
               "naive": "#999999"}


def _fig(data, out_dir):
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(11, 4.2))

    # ---- left: mechanism costs ----
    mech = [m for m in data.get("mechanisms", [])
            if isinstance(m.get("cyc_per_call"), (int, float))
            and m["cyc_per_call"] > 0]
    if mech:
        labels = [m["label"] for m in mech]
        vals = [m["cyc_per_call"] for m in mech]
        colors = [GROUP_COLOR.get(m["group"], "#777777") for m in mech]
        ypos = range(len(labels))
        axL.barh(list(ypos), vals, color=colors)
        axL.set_yticks(list(ypos))
        axL.set_yticklabels(labels, fontsize=9)
        axL.invert_yaxis()
        axL.set_xscale("log")
        axL.set_xlabel("cycles per call (log)")
        axL.set_title("Cost of each return lowering")
        for y, v in zip(ypos, vals):
            axL.text(v * 1.08, y, "%.1f" % v, va="center", fontsize=8)
        axL.margins(x=0.25)
    else:
        axL.set_axis_off()
        axL.text(0.5, 0.5, "no mechanism data", ha="center", va="center",
                 transform=axL.transAxes, fontsize=10, color="#999999")

    # ---- right: depth sweep ----
    d = data.get("depth", {})
    xs = d.get("depths", [])
    cr = d.get("call_ret", [])
    mh = d.get("meta_ho", [])
    if xs and cr and mh:
        axR.plot(xs, cr, "o-", color="#4C72B0", label="call / ret")
        axR.plot(xs, mh, "s-", color="#DD8452", label="metamorphic (hoisted)")
        axR.set_yscale("log")
        axR.set_xlabel("call-chain depth")
        axR.set_ylabel("cycles per outer iteration (log)")
        axR.set_title("Deep chains: return-stack-buffer crossover")
        axR.legend(fontsize=9)
        # shade where metamorphic wins
        cross = None
        for i in range(len(xs)):
            if mh[i] < cr[i]:
                cross = xs[i]
                break
        if cross is not None:
            axR.axvspan(cross, xs[-1], color="#DD8452", alpha=0.08)
            axR.text(cross, min(min(cr), min(mh)),
                     " metamorphic\n wins",
                     fontsize=8, color="#B5651D", va="bottom")
    else:
        axR.set_axis_off()
        axR.text(0.5, 0.5, "no depth-sweep data", ha="center", va="center",
                 transform=axR.transAxes, fontsize=10, color="#999999")
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(out_dir, "metamorphic_bench." + ext),
                    dpi=150)
    plt.close(fig)


def _body(data, out_dir):
    d = data.get("depth", {})
    xs = d.get("depths", [])
    cr = d.get("call_ret", [])
    mh = d.get("meta_ho", [])
    lines = []
    lines.append("The two panels answer two questions. Left: what does it cost "
                 "to lower a function's \\emph{return}? An ordinary "
                 "\\texttt{call}/\\texttt{ret} is the reference. Routing the "
                 "return through a memory slot (\\texttt{jmp [slot]}) is a "
                 "little slower because of the load; routing it through a "
                 "register whose target was patched into a \\texttt{mov} "
                 "immediate (\\texttt{jmp reg}) is faster than the slot and "
                 "close to \\texttt{call}/\\texttt{ret}. Patching that "
                 "immediate on \\emph{every} call instead of once is a "
                 "self-modifying-code machine clear --- two orders of magnitude "
                 "worse --- which is why the patch must be hoisted out of the "
                 "loop.\n")
    lines.append("Right: a balanced \\texttt{call}/\\texttt{ret} is predicted "
                 "by the CPU's return-stack buffer only while the chain fits in "
                 "it. Past that depth the outermost returns mispredict, while a "
                 "monomorphic \\texttt{jmp reg} is predicted at any depth, so "
                 "deep chains tip in metamorphic's favour.\n")
    if xs:
        lines.append("\\begin{center}")
        lines.append("\\begin{tabular}{rrrl}")
        lines.append("\\toprule")
        lines.append("depth & call/ret & metamorphic & winner \\\\")
        lines.append("\\midrule")
        for i, dep in enumerate(xs):
            win = "metamorphic" if mh[i] < cr[i] else "call/ret"
            lines.append("%d & %.1f & %.1f & %s \\\\" %
                         (dep, cr[i], mh[i], win))
        lines.append("\\bottomrule")
        lines.append("\\end{tabular}")
        lines.append("\\end{center}")
        lines.append("\nCycles per outer iteration; each row is a chain of that "
                     "depth. The crossover sits near the return-stack buffer's "
                     "capacity.\n")
    with open(os.path.join(out_dir, "metamorphic_body.tex"), "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/shivyc_benchmarks"
    os.makedirs(out_dir, exist_ok=True)
    with open(RESULTS) as f:
        data = json.load(f)
    _fig(data, out_dir)
    _body(data, out_dir)
    print("metamorphic figures ->", out_dir)


if __name__ == "__main__":
    main()
