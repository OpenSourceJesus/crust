#!/usr/bin/env python3
"""Build, package, run and test for the Raspberry Pi.

Takes your source files, compiles them with everything a 64-bit Pi needs,
packages the result so the directory can be copied to the board, and -- with
--qemu -- runs it here under a Cortex-A72 (the Pi 4's core).

    python3 tools/raspi.py prog.c --qemu
    python3 tools/raspi.py prog.c --selfhosted --qemu     # no gcc needed
    python3 tools/raspi.py prog.c --test-script=t.py --debug
    python3 tools/raspi.py --info

The Pi 3, 4, 5 and Zero 2 W are all ARMv8 and use the same AArch64 target; the
Pi 1, Zero and Zero W are ARMv6 and are not supported at all. What catches
people out is the *OS*: a 64-bit-capable Pi running 32-bit Pi OS reports
armv7l and has no back end here. See BOARDS.md.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_common as bc        # noqa: E402

# Cortex-A72 is the Pi 4/400's core. The A53 (Pi 3, Zero 2 W) and A76 (Pi 5)
# are also ARMv8-A and run the same baseline code we emit; A72 is the middle
# of the range and the most widely deployed, so it is the default here.
RASPI = bc.Board(
    key="raspi",
    name="Raspberry Pi 4",
    cpu="cortex-a72",
    target="arm64",
    qemu="qemu-aarch64",
    notes=[
        "needs a 64-bit OS: 32-bit Pi OS reports armv7l and is unsupported",
        "Pi 1 / Zero / Zero W are ARMv6 and cannot run this at all",
        "qemu-user emulates the CPU and syscalls, not the board: no GPIO, "
        "no device tree, no firmware",
        "4 KB pages assumed, which is what 64-bit Pi OS uses",
    ])

if __name__ == "__main__":
    sys.exit(bc.main(RASPI, sys.argv))
