#!/usr/bin/env python3
"""Build, package, run and test for the NVIDIA Jetson Nano.

Takes your source files, compiles them with everything the Nano needs,
packages the result so the directory can be copied to the board, and -- with
--qemu -- runs it here under a Cortex-A57 (the Tegra X1's core).

    python3 tools/jetnano.py prog.c --qemu
    python3 tools/jetnano.py prog.c --selfhosted --qemu   # no gcc needed
    python3 tools/jetnano.py prog.c --test-script=t.py --debug
    python3 tools/jetnano.py --info

The Nano runs L4T (Ubuntu), which is 64-bit, so unlike the Raspberry Pi there
is no 32-bit-userland trap here. Nothing we emit is Tegra-specific: the CUDA
and multimedia stacks are libraries, not instructions, and are outside what
this compiler covers.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_common as bc        # noqa: E402

# Tegra X1 (T210): four Cortex-A57 cores, ARMv8-A. Xavier and Orin are
# different cores but the same AArch64 baseline, so this profile works for
# them too -- only the -cpu model would change.
JETSON_NANO = bc.Board(
    key="jetnano",
    name="Jetson Nano",
    cpu="cortex-a57",
    target="arm64",
    qemu="qemu-aarch64",
    notes=[
        "L4T is 64-bit, so there is no 32-bit-userland trap as on the Pi",
        "qemu-user emulates the CPU and syscalls, not the board: no CUDA, "
        "no GPU, no Tegra peripherals",
        "4 KB pages assumed, which is what L4T uses",
        "Xavier / Orin work from this same profile with a different -cpu",
    ])

if __name__ == "__main__":
    sys.exit(bc.main(JETSON_NANO, sys.argv))
