#!/usr/bin/env python3
"""Run the metamorphic-return benchmarks and write results/metamorphic_results.json
for plot_metamorphic.py. The benchmark generators live in tools/metamorphic and
build through the self-hosted rasm + rlink path (no external as/ld); this harness
just drives them and parses the cyc/call each variant prints.

Two studies are recorded:
  * mechanisms -- the cost of each way to lower a return (call/ret, memory slot,
    register jump, per-call patch, push;ret trampoline), showing which lowering
    is correct.
  * depth      -- call/ret vs hoisted metamorphic as the call chain deepens,
    showing the return-stack-buffer crossover where metamorphic wins.

Iteration counts are tunable via $METAMORPHIC_N (smaller = faster, for the report
build). Env $METAMORPHIC_DEPTHS overrides the depth sweep.
"""
import json
import os
import re
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
META = os.path.join(ROOT, "tools", "metamorphic")
RESULTS = os.path.join(HERE, "results", "metamorphic_results.json")

os.environ.setdefault("METAMORPHIC_N", "12000000")
sys.path.insert(0, META)

CPC = re.compile(r":\s*\d+\s*cyc\s+([\d.]+)")


def _ensure_rlibc():
    """rbuild needs tools/rpy_lib/build/rlibc.o; trigger a build if missing."""
    obj = os.path.join(ROOT, "tools", "rpy_lib", "build", "rlibc.o")
    if os.path.exists(obj):
        return
    src = os.path.join(HERE, "_meta_probe.c")
    with open(src, "w") as f:
        f.write("int main(){return 0;}\n")
    env = dict(os.environ, SHIVYC_RASM="1", SHIVYC_RLINK="1",
               SHIVYC_BASE="0x1000", PYTHONPATH=ROOT)
    subprocess.run([sys.executable, "-m", "shivyc.main", src, "-o",
                    os.path.join(HERE, "_meta_probe")],
                   cwd=ROOT, env=env, capture_output=True)


def _build(gen, asm, out):
    subprocess.run([sys.executable, os.path.join(META, gen), asm],
                   cwd=META, check=True, capture_output=True)
    subprocess.run([sys.executable, os.path.join(META, "rbuild.py"),
                    asm, out, "0x1000"], cwd=META, check=True,
                   capture_output=True)


def _run_medians(binary, runs=5):
    """Return {variant_prefix: median cyc/call} by parsing the binary's output."""
    series = {}
    for _ in range(runs):
        out = subprocess.run([binary], cwd=META, capture_output=True,
                             text=True).stdout
        for line in out.splitlines():
            m = CPC.search(line)
            if not m:
                continue
            key = line.split()[0]
            series.setdefault(key, []).append(float(m.group(1)))
    return {k: statistics.median(v) for k, v in series.items()}


def mechanisms():
    _build("gen_immret.py", os.path.join(META, "bench_immret.s"),
           os.path.join(META, "bench_immret"))
    imm = _run_medians(os.path.join(META, "bench_immret"))
    _build("gen_trampoline.py", os.path.join(META, "bench_trampoline.s"),
           os.path.join(META, "bench_trampoline"))
    tr = _run_medians(os.path.join(META, "bench_trampoline"))
    # map variant prefixes to human labels + a group used for colouring
    rows = [
        ("call / ret", imm.get("D"), "reference"),
        (r"jmp [slot], hoisted", imm.get("SLOT_HO"), "metamorphic"),
        (r"jmp reg, hoisted", imm.get("IMM_HO"), "metamorphic"),
        (r"push;ret trampoline", tr.get("R"), "naive"),
        (r"jmp reg, patch/call", imm.get("IMM_PC"), "naive"),
    ]
    return [{"label": l, "cyc_per_call": v, "group": g}
            for l, v, g in rows if v is not None]


def depth_sweep():
    import bench_deep
    depths = [int(x) for x in
              os.environ.get("METAMORPHIC_DEPTHS", "4 8 16 24 32 48 64").split()]
    call, meta = [], []
    for d in depths:
        c, m = bench_deep.run(d)
        call.append(c)
        meta.append(m)
    return {"depths": depths, "call_ret": call, "meta_ho": meta}


def main():
    _ensure_rlibc()
    data = {"n": int(os.environ["METAMORPHIC_N"])}
    try:
        data["mechanisms"] = mechanisms()
    except Exception as e:
        sys.stderr.write("metamorphic mechanisms failed: %s\n" % e)
        data["mechanisms"] = []
    try:
        data["depth"] = depth_sweep()
    except Exception as e:
        sys.stderr.write("metamorphic depth sweep failed: %s\n" % e)
        data["depth"] = {"depths": [], "call_ret": [], "meta_ho": []}

    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    with open(RESULTS, "w") as f:
        json.dump(data, f, indent=2)
    print("wrote", RESULTS)
    for r in data["mechanisms"]:
        print("  %-24s %8.2f cyc/call" % (r["label"], r["cyc_per_call"]))
    d = data["depth"]
    for i, dep in enumerate(d["depths"]):
        print("  depth %3d: call/ret %8.2f  meta %8.2f" %
              (dep, d["call_ret"][i], d["meta_ho"][i]))


if __name__ == "__main__":
    main()
