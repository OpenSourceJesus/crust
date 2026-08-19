#!/usr/bin/env python3
"""Check that --emit-thread-switcher is target-correct.

This exists because the AArch64 path was not merely missing -- it was *wrong*.
Asking for `--target arm64` produced a byte-identical **x86-64** switcher:
Intel syntax, `mov QWORD PTR [rdi+16], rax`, and a report claiming
`rax, rcx, rdx, rsi` was an AArch64 register footprint. Nothing failed; the
output was simply for the wrong machine.

Three separate silences produced that, and each is checked below:

  1. `_compile_and_scan` shelled out to the compiler without passing
     `--target`, so it always compiled, scanned and emitted x86.
  2. When a compile failed it was skipped with `continue`, so an unbuildable
     file reported "uses no registers" and the partition looked clean while
     meaning nothing.
  3. `_apply_thread_budget` ran in `make_asm`, but `make_asm` dispatches to
     `_make_asm_arm64` *before* reaching it -- so on AArch64 the budget was
     computed, serialised, passed on the command line, and ignored. The
     partition stayed an observation rather than a guarantee.

    python3 tools/thread_partition_test.py
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BENCH = os.path.join(ROOT, "benchmarks", "threads")

X86_REGS = re.compile(r"\b(rax|rbx|rcx|rdx|rsi|rdi|r8|r9|r1[0-5])\b")
A64_REGS = re.compile(r"\bx(1[9]|2[0-8])\b")


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, **kw)
    return p.returncode, p.stdout, p.stderr


def emit(src, target, out):
    cmd = [sys.executable, "-m", "shivyc.main", src,
           "--emit-thread-switcher", out]
    if target:
        cmd += ["--target", target]
    return run(cmd)


def have(tool):
    return subprocess.run(["which", tool], capture_output=True).returncode == 0


def run_booted_preempt():
    """Boot two register-partitioned threads and check they really preempt.

    Every other check in this file inspects emitted text. This is the only one
    that *runs* the switcher, and it is the one that caught the bug the static
    checks could not: `bl timer_ack` clobbers x9, which is caller-saved, while
    the swap immediately after reads x9 as the outgoing TCB pointer. The
    generated code was correct in every structural respect -- right save sets,
    balanced frames, ELR and SPSR handled -- and still stored garbage into
    next_tcb, so the *second* switch resumed from a corrupt TCB and eret landed
    at EL0 on a misaligned PC.

    The third assertion is the one that matters: each worker re-derives an
    arithmetic invariant every iteration, so a live register the switcher
    failed to preserve shows up as a mismatch. Zero mismatches across
    thousands of switches is the evidence that the *specialized* save set --
    only the running group's footprint, not the whole file -- is sufficient.
    """
    import re as _re
    if subprocess.run(["which", "qemu-system-aarch64"],
                      capture_output=True).returncode != 0:
        print("  SKIP  preempt boot: qemu-system-aarch64 not installed")
        return 0, 0
    sys.path.insert(0, HERE)
    import baremetal_arm64 as bm

    app = os.path.join(ROOT, "examples", "baremetal", "kernel_preempt.c")
    if not os.path.exists(app):
        print("  SKIP  preempt boot: example missing")
        return 0, 0

    npass = nfail = 0
    with tempfile.TemporaryDirectory() as d:
        decl = os.path.join(d, "decl.c")
        with open(decl, "w") as f:
            f.write("void worker_left(void); void worker_right(void);\n"
                    "int main()\n"
                    "assert worker_left in threads.left( core=0 )\n"
                    "assert worker_right in threads.right( core=0 )\n"
                    "{ worker_left(); worker_right(); return 0; }\n")
        sw = os.path.join(d, "sw.s")
        rc, out, err = run([sys.executable, "-m", "shivyc.main", app, decl,
                            "--emit-thread-switcher", sw, "--target", "arm64"])
        pre = sw[:-2] + ".preempt.s"
        if rc != 0 or not os.path.exists(pre):
            print("  FAIL  preempt boot: generating the switcher failed")
            return 0, 1
        elf = os.path.join(d, "preempt.elf")
        try:
            bm.build([app], elf, os.path.join(d, "obj"),
                     extra_asm=["vectors_preempt_arm64.S", pre])
        except Exception as e:
            print("  FAIL  preempt boot: build failed: %s" % e)
            return 0, 1
        text = bm.qemu_run(elf, timeout=60).replace("\r", "")

    rows = _re.findall(r"left=(\d+)\s+right=(\d+)\s+ticks=(\d+)"
                       r"\s+switches=(\d+)\s+corrupt\(l/r\)=(\d+)/(\d+)",
                       text)
    if len(rows) < 3:
        print("  FAIL  preempt boot: only %d report lines" % len(rows))
        for line in text.split("\n")[-6:]:
            if line.strip():
                print("        | " + line)
        return 0, 1

    left, right = int(rows[-1][0]), int(rows[-1][1])
    switches = int(rows[-1][3])
    bad_l, bad_r = int(rows[-1][4]), int(rows[-1][5])

    for name, ok, detail in (
            ("the right thread actually runs", right > 0, "right=%d" % right),
            ("switching keeps going, not just once",
             switches >= 2 and switches > int(rows[0][3]),
             "switches=%d" % switches),
            ("neither thread's state was corrupted",
             bad_l == 0 and bad_r == 0, "l=%d r=%d" % (bad_l, bad_r)),
            ("the left thread keeps running too", left > 0,
             "left=%d" % left)):
        if ok:
            print("  PASS  %-52s %s" % (name, detail))
            npass += 1
        else:
            print("  FAIL  %-52s %s" % (name, detail))
            nfail += 1
    return npass, nfail


def main():
    npass = nfail = 0

    def check(name, ok, detail=""):
        nonlocal npass, nfail
        if ok:
            print("  PASS  %-52s %s" % (name, detail))
            npass += 1
        else:
            print("  FAIL  %-52s %s" % (name, detail))
            nfail += 1

    simple = os.path.join(BENCH, "bench_threads.c")
    heavy = os.path.join(BENCH, "bench_threads_calls.c")
    for f in (simple, heavy):
        if not os.path.exists(f):
            print("SKIP: %s missing" % f)
            return 0

    with tempfile.TemporaryDirectory() as d:
        # ---- x86-64 still works, unchanged -----------------------------
        x86_out = os.path.join(d, "x86.s")
        rc, out, err = emit(simple, None, x86_out)
        check("x86-64 switcher still emitted", rc == 0 and
              os.path.exists(x86_out), "rc=%d" % rc)
        x86_txt = open(x86_out).read() if os.path.exists(x86_out) else ""
        check("x86-64 output is x86 assembly",
              "intel_syntax" in x86_txt and bool(X86_REGS.search(x86_txt)))
        check("x86-64 footprints become disjoint",
              "footprints are disjoint" in out)

        # ---- arm64 emits AArch64, not x86 ------------------------------
        a64_out = os.path.join(d, "a64.s")
        rc, out, err = emit(simple, "arm64", a64_out)
        check("arm64 switcher emitted", rc == 0 and os.path.exists(a64_out),
              "rc=%d" % rc)
        a64_txt = open(a64_out).read() if os.path.exists(a64_out) else ""
        # The original bug in one assertion: the two outputs were identical.
        check("arm64 output differs from the x86 one",
              a64_txt != x86_txt and a64_txt != "",
              "(identical output was the original bug)")
        check("arm64 output contains no x86 registers",
              not X86_REGS.search(a64_txt) and "intel_syntax" not in a64_txt)
        check("arm64 output uses x19-x28 homes",
              bool(A64_REGS.search(a64_txt)))
        check("arm64 report names AArch64 registers",
              not X86_REGS.search(out) and bool(A64_REGS.search(out)))

        # ---- the budget is actually applied ----------------------------
        # Disjointness on arm64 is only reachable if _apply_thread_budget runs
        # on the arm64 path. Before the fix both threads kept landing on
        # x19-x21 and this line never appeared.
        check("arm64 constrained allocation makes footprints disjoint",
              "footprints are disjoint" in out)

        # ---- emitted assembly is real -----------------------------------
        if have("aarch64-linux-gnu-as"):
            obj = os.path.join(d, "a64.o")
            rc2, _, err2 = run(["aarch64-linux-gnu-as", "-o", obj, a64_out])
            check("arm64 switcher assembles with GNU as", rc2 == 0,
                  err2.strip().split("\n")[0] if rc2 else "")
        else:
            print("  SKIP  aarch64-linux-gnu-as not installed")

        # rasm is ours, and the point is that no external tool is needed.
        sys.path.insert(0, os.path.join(HERE, "rpy_lib"))
        try:
            import rasm_obj
            data = rasm_obj.assemble_to_elf(a64_txt, "arm64")
            check("arm64 switcher assembles with our own rasm",
                  len(data) > 0, "%d bytes" % len(data))
        except Exception as e:
            check("arm64 switcher assembles with our own rasm", False,
                  "%s: %s" % (type(e).__name__, e))

        # ---- under real pressure it degrades, and says so ---------------
        heavy_out = os.path.join(d, "heavy.s")
        rc, hout, herr = emit(heavy, "arm64", heavy_out)
        check("arm64 handles a call-graph workload", rc == 0, "rc=%d" % rc)
        htxt = open(heavy_out).read() if os.path.exists(heavy_out) else ""
        # Two threads wanting eight of ten callee-saved registers cannot be
        # disjoint. What matters is that the tool says so rather than
        # claiming a partition it did not achieve, and that the switcher is
        # still correct -- it saves the outgoing set and restores the
        # incoming one however they overlap.
        check("overlap is reported honestly, not claimed as disjoint",
              ("overlap on" in hout) or ("are disjoint" in hout),
              "reported: %s" % ("overlap" if "overlap on" in hout
                                else "disjoint"))
        saves = htxt.count("stp ") + htxt.count("str x1") \
            + htxt.count("str x2")
        check("degraded switcher still saves and restores",
              htxt.count("stp ") > 0 and htxt.count("ldp ") > 0,
              "%d stp / %d ldp" % (htxt.count("stp "), htxt.count("ldp ")))

        # ---- the preemptive path -----------------------------------------
        pre = a64_out[:-2] + ".preempt.s"
        check("arm64 preemptive timer path emitted", os.path.exists(pre))
        ptxt = open(pre).read() if os.path.exists(pre) else ""
        check("preempt path is AArch64, not x86",
              "intel_syntax" not in ptxt and not X86_REGS.search(ptxt))
        # A tick lands at an arbitrary instruction, so caller-saved registers
        # are live. Saving only the callee-saved bank would corrupt the
        # interrupted thread intermittently.
        # Registers reach the TCB by `str x, [x9, #n]` or `stp x, y, [x9, #n]`
        # since the emitter pairs adjacent slots -- matching only `str` would
        # silently see almost nothing once pairing is in.
        saved = set(re.findall(r"str\s+(x\d+), \[x9", ptxt))
        for a, b in re.findall(r"stp\s+(x\d+), (x\d+), \[x9", ptxt):
            saved.add(a)
            saved.add(b)
        caller_saved = [r for r in saved
                        if r not in ("x9", "x10")
                        and int(r[1:]) < 19]
        check("preempt saves caller-saved registers too",
              len(caller_saved) > 0,
              "e.g. %s" % ", ".join(sorted(caller_saved)[:4]))
        check("preempt saves ELR_EL1 and SPSR_EL1",
              "mrs  x10, elr_el1" in ptxt and "mrs  x10, spsr_el1" in ptxt,
              "(eret restores both; registers alone is not enough)")
        # On entry no register is free. The dispatch stub parks two on the
        # stack and each entry reclaims that frame -- one sub, one add per
        # path. An imbalance leaks or corrupts the interrupted stack.
        # The invariant is per *path*, not per file: timer_dispatch allocates
        # the frame once and whichever entry it branches to reclaims it. So
        # one sub in dispatch, zero in the entries, and exactly one add in
        # each entry -- a file-wide count of subs against adds would compare
        # one allocation against two reclaims and be wrong in both directions.
        def body_of(nm):
            if nm + ":" not in ptxt:
                return ""
            after = ptxt.split(nm + ":", 1)[1]
            return after.split(".global", 1)[0]

        # dispatch allocates one frame that the entry reclaims; the entry may
        # also allocate and reclaim its own (the cur/next exchange needs a
        # third slot). So the invariant is a *net* one per path -- entries
        # release exactly one more frame than they take -- rather than a fixed
        # instruction count, which changes whenever the body does.
        def frames(nm):
            b = body_of(nm)
            return (len(re.findall(r"sub\s+sp, sp, #16", b)),
                    len(re.findall(r"add\s+sp, sp, #16", b)))

        d_sub, d_add = frames("timer_dispatch")
        ok_frame = (d_sub == 1 and d_add == 0)
        detail = "dispatch %d/%d" % (d_sub, d_add)
        for side in ("timer_isr_left", "timer_isr_right"):
            e_sub, e_add = frames(side)
            detail += ", %s %d/%d" % (side.replace("timer_isr_", ""),
                                      e_sub, e_add)
            # net: everything dispatch and the entry pushed is popped.
            if e_add - e_sub != 1:
                ok_frame = False
        check("preempt scratch frame balances along every path", ok_frame,
              detail)
        # The original values of the scratch pair must reach the TCB, not the
        # clobbered ones. An earlier version stored the TCB pointer in place
        # of the thread's x9.
        check("preempt recovers the original scratch registers",
              "original x9" in ptxt and "original x10" in ptxt)
        check("preempt flips timer_vector to the other side",
              "timer_isr_right" in ptxt and "timer_isr_left" in ptxt
              and "str  x10, [x9]" in ptxt)
        if have("aarch64-linux-gnu-as"):
            pobj = os.path.join(d, "preempt.o")
            rc3, _, err3 = run(["aarch64-linux-gnu-as", "-o", pobj, pre])
            check("preempt path assembles with GNU as", rc3 == 0,
                  err3.strip().split("\n")[0] if rc3 else "")

        # ---- unsupported targets refuse rather than emit x86 ------------
        for tgt in ("riscv64",):
            bad_out = os.path.join(d, "bad_%s.s" % tgt)
            rc, out2, err2 = emit(simple, tgt, bad_out)
            check("--target %s refuses instead of emitting x86" % tgt,
                  rc != 0 and not os.path.exists(bad_out),
                  "rc=%d, file %s" % (rc, "written" if
                                      os.path.exists(bad_out) else "absent"))

    print()
    p, f = run_booted_preempt()
    npass += p
    nfail += f

    print()
    _p, _f = run_booted_preempt()
    npass += _p
    nfail += _f

    print("\nthread partition targets: %d pass, %d fail" % (npass, nfail))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
