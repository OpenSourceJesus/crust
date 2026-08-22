"""Boot the boards qemu cannot model, under armulator, and check the results.

virt and the Pi 3 have qemu machines and are covered by irq_timer_test.py.
The Jetson and the Pi 4 do not, so until armulator they were built and never
run -- which is how an MMU map that could not describe the Jetson's RAM
survived, and why the Pi 4's GIC-400 sat behind ``irq: False``.

This checks the parts that only show up when the image actually runs:

  * it reaches the end of kmain rather than faulting through the vectors
  * CNTFRQ_EL0 is the rate that board really clocks its timer at
  * the deliberate bad store aborts with the MMU off, instead of being
    quietly swallowed by an unmapped write
  * timer interrupts arrive at the rate asked for, with none spurious

Skips cleanly when armulator is not checked out.

    python3 tools/armulator_boards_test.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

APP = os.path.join(ROOT, "examples", "baremetal", "kernel_arm64.c")

#: board -> CNTFRQ_EL0 the hardware really uses.
EXPECTED_FREQUENCY = {
    "jetson": 19200000,     # Tegra X1
    "raspi4": 54000000,     # BCM2711, and not the Pi 3's 19.2 MHz
}


def check(results, name, condition, detail=""):
    results.append((name, bool(condition), detail))


def run_board(board):
    import jetson_armulator as ja
    import tempfile

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        elf = ja.build_image(APP, os.path.join(tmp, board + ".elf"),
                             board=board)
        text, machine, executed = ja.boot(
            elf, ram_size=0x400000, quiet=True, expect="all stages ok",
            board_name=board, max_instructions=4000000)

    clean = text.replace("\r", "")

    check(results, "reaches the end of kmain", "all stages ok" in clean,
          repr(clean[-160:]))
    check(results, "does not fault through the vectors",
          not machine.fault_loop)

    # CNTFRQ is a board property. Getting it wrong scales every delay.
    match = re.search(r"timer freq\s*=\s*(\d+)", clean)
    frequency = int(match.group(1)) if match else None
    check(results, "CNTFRQ_EL0 matches the board",
          frequency == EXPECTED_FREQUENCY[board],
          "got %r, expected %d" % (frequency, EXPECTED_FREQUENCY[board]))

    # Two faults: the bad store with the MMU off, then the same store
    # translated. The first only counts if unmapped writes abort rather than
    # vanishing, which is what qemu does and what this model now does too.
    match = re.search(r"faults total:\s*(\d+)", clean)
    faults = int(match.group(1)) if match else None
    check(results, "unmapped store aborts with the MMU off", faults == 2,
          "faults total = %r, expected 2" % faults)
    check(results, "external abort, not a translation fault",
          "external abort" in clean)

    # The interrupt path itself.
    match = re.search(r"unmasked, waiting 300ms:\s*(\d+) ticks", clean)
    ticks = int(match.group(1)) if match else None
    check(results, "timer interrupts arrive at ~100 Hz",
          ticks is not None and 25 <= ticks <= 35,
          "ticks = %r, expected about 30" % ticks)
    check(results, "no spurious interrupts", "spurious=0" in clean)
    check(results, "no interrupts from unexpected sources",
          "unexpected=0" in clean)

    return results


def main():
    try:
        import jetson_armulator as ja
        ja.find_armulator()
    except SystemExit:
        print("SKIP: armulator not checked out "
              "(git clone https://github.com/crustos/armulator ../armulator)")
        return 0

    total_pass = total_fail = 0
    for board in sorted(EXPECTED_FREQUENCY):
        print("\n== %s ==" % board)
        try:
            results = run_board(board)
        except Exception as exc:
            print("  FAIL  [%s] %s: %s" % (board, type(exc).__name__, exc))
            total_fail += 1
            continue
        for name, ok, detail in results:
            if ok:
                print("  PASS  %s" % name)
                total_pass += 1
            else:
                print("  FAIL  %s  %s" % (name, detail))
                total_fail += 1

    print("\narmulator boards: %d pass, %d fail" % (total_pass, total_fail))
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
