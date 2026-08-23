"""hostsim_difftest.py - check the fast path still agrees with the emulator.

    python3 tools/hostsim_difftest.py

The host path replaces the hardware layer with a model written by hand. That
model can drift from the drivers it stands in for, and when it does the
failure is the worst kind: the fast path keeps passing while the real image
has stopped working, or the other way round, and nothing says so.

So the same application is run both ways -- natively under hostsim, and
instruction by instruction under armulator on a board qemu cannot model -- and
the results are compared on the things both are supposed to agree about:

  * what the computation produced (fib, the sum through the MMU)
  * how many faults were taken and recovered from
  * how many timer interrupts arrived at a given rate, and that none were
    spurious or from an unexpected source

The two do *not* agree about everything, and the differences are the point of
having both. armulator reports ESR and FAR for each fault, has real exception
levels, and executes ARM; hostsim has none of that and reports the counters
only. Comparing raw console text would fail on those differences and teach
everyone to ignore the test, so specific facts are extracted and compared.

Skips cleanly when armulator is not checked out.
"""

import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

APP = os.path.join(ROOT, "examples", "baremetal", "kernel_arm64.c")

#: Facts both back ends must report identically, and how to find them.
FACTS = {
    "fib": r"fib\(0\.\.12\):\s*([0-9 ]+?)\s*\n",
    "mmu_sum": r"summing an array through the MMU:\s*(\d+)",
    "faults_total": r"faults total:\s*(\d+)",
    "ticks_masked": r"waiting 200ms:\s*(\d+) ticks",
    "ticks_unmasked": r"waiting 300ms:\s*(\d+) ticks",
    "spurious": r"spurious=(\d+)",
    "unexpected": r"unexpected=(\d+)",
    "completed": r"(== all stages ok ==)",
}

#: Timer interrupts are counted over a nominal 300ms at 100Hz. Neither back
#: end lands on exactly 30 -- the emulator's counter advances per instruction
#: and the host's per grant -- so this one is compared with a tolerance.
TOLERANT = {"ticks_unmasked": 5}


def extract(text):
    clean = text.replace("\r", "")
    facts = {}
    for name, pattern in FACTS.items():
        match = re.search(pattern, clean)
        facts[name] = match.group(1).strip() if match else None
    return facts


def run_hostsim(step_ms=1, limit=5000):
    from hostsim import Sim
    import hostsim_build

    so = os.path.join(tempfile.mkdtemp(prefix="hostsim-diff-"), "app.so")
    hostsim_build.build([APP], so, verbose=False)

    sim = Sim(so)
    sim.start()
    chunks = []
    for _ in range(limit):
        sim.step_ms(step_ms)
        chunks.append(sim.read())
        if sim.finished:
            break
    chunks.append(sim.read())
    sim.close()
    return "".join(chunks)


def run_armulator(board="jetson"):
    import jetson_armulator as ja

    tmp = tempfile.mkdtemp(prefix="armulator-diff-")
    elf = ja.build_image(APP, os.path.join(tmp, board + ".elf"), board=board)
    text, _machine, _n = ja.boot(
        elf, ram_size=0x400000, quiet=True, expect="all stages ok",
        board_name=board, max_instructions=4000000)
    return text


def main():
    try:
        import jetson_armulator as ja
        ja.find_armulator()
    except SystemExit:
        print("SKIP: armulator not checked out "
              "(git clone https://github.com/crustos/armulator ../armulator)")
        return 0

    print("== running %s both ways ==" % os.path.basename(APP))
    try:
        host_text = run_hostsim()
    except SystemExit as exc:
        print("  FAIL  hostsim build/run: %s" % exc)
        return 1
    emu_text = run_armulator()

    host = extract(host_text)
    emu = extract(emu_text)

    passed = failed = 0
    print()
    for name in FACTS:
        h, e = host.get(name), emu.get(name)
        if h is None or e is None:
            print("  FAIL  %-16s missing: hostsim=%r armulator=%r"
                  % (name, h, e))
            failed += 1
            continue

        tolerance = TOLERANT.get(name)
        if tolerance is not None:
            try:
                ok = abs(int(h) - int(e)) <= tolerance
            except ValueError:
                ok = h == e
            detail = "%s vs %s (tolerance %d)" % (h, e, tolerance)
        else:
            ok = h == e
            detail = "%s" % h if ok else "hostsim=%r armulator=%r" % (h, e)

        if ok:
            print("  PASS  %-16s %s" % (name, detail))
            passed += 1
        else:
            print("  FAIL  %-16s %s" % (name, detail))
            failed += 1

    print("\nhostsim vs armulator: %d pass, %d fail" % (passed, failed))
    if failed:
        print("\nThe fast path and the emulator disagree. Either the host "
              "model in hostsim/hostsim.c has drifted from the driver it "
              "stands in for, or a real change in behaviour has been made "
              "and only one path has it.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
