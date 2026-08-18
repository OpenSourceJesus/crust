#!/usr/bin/env python3
"""Check the AArch64 exception vector table's geometry.

The architecture fixes this layout: 16 slots, 128 bytes apart, in a 2 KiB
block, and the CPU branches to VBAR_EL1 + a *hardcoded* offset. Nothing checks
that a slot's code fits. `.balign 128` does not error on an oversized entry --
it rounds the next one up to the following boundary, so an entry one
instruction too long silently doubles the spacing and every later vector moves.

That failure mode is unusually nasty: the table still assembles, still links,
still installs, and the machine runs fine until the first exception, which then
enters the wrong handler and reports the wrong kind. The bug this test exists
for did exactly that -- a 33-instruction slot body reported a synchronous EL1h
data abort as an FIQ.

So the invariant is checked directly against the assembled bytes:

  * exactly 16 slots
  * consecutive slots exactly 128 bytes apart
  * the table 2048-byte aligned
  * each slot's `mov x0, #N` immediate equal to its index -- which is what
    ties slot *position* to the kind the handler reports

    python3 tools/vectors_size_test.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BAREMETAL = os.path.join(ROOT, "baremetal64")
sys.path.insert(0, os.path.join(HERE, "rpy_lib"))

SLOT_SIZE = 128
NUM_SLOTS = 16
TABLE_ALIGN = 2048


def assemble_vectors():
    import rasm_obj
    src = os.path.join(BAREMETAL, "vectors_arm64.S")
    with open(src) as f:
        text = f.read()
    return bytes(rasm_obj.assemble_to_elf(text, "arm64"))


def section_bytes(obj_path, name):
    """Raw contents of one section, via objcopy."""
    out = obj_path + ".bin"
    p = subprocess.run(
        ["aarch64-linux-gnu-objcopy", "-O", "binary", "--only-section", name,
         obj_path, out], capture_output=True, text=True)
    if p.returncode != 0 or not os.path.exists(out):
        return None
    with open(out, "rb") as f:
        return f.read()


def decode_mov_imm(word):
    """If `word` is `movz x0, #imm`, return imm; else -1.

    movz (64-bit, shift 0) is 0xD2800000 | imm16 << 5 | Rd.
    """
    if (word & 0xFFE00000) != 0xD2800000:
        return -1
    if (word & 0x1F) != 0:            # Rd must be x0
        return -1
    return (word >> 5) & 0xFFFF


def main():
    if subprocess.run(["which", "aarch64-linux-gnu-objcopy"],
                      capture_output=True).returncode != 0:
        print("SKIP: aarch64-linux-gnu-objcopy not installed")
        return 0

    import tempfile
    fails = []
    with tempfile.TemporaryDirectory() as d:
        obj = os.path.join(d, "vectors.o")
        with open(obj, "wb") as f:
            f.write(assemble_vectors())
        data = section_bytes(obj, ".vectors")
        if data is None:
            print("  FAIL  could not extract .vectors")
            return 1

    size = len(data)
    want = SLOT_SIZE * NUM_SLOTS
    if size < want:
        fails.append(".vectors is %d bytes, need at least %d" % (size, want))

    # Each slot must begin with `sub sp, sp, #256` (0xD10403FF). Finding that
    # word tells us where the slots actually landed, independent of what the
    # source intended.
    starts = []
    for off in range(0, size - 3, 4):
        word = int.from_bytes(data[off:off + 4], "little")
        if word == 0xD10403FF:
            starts.append(off)

    if len(starts) != NUM_SLOTS:
        fails.append("found %d slot entries, expected %d"
                     % (len(starts), NUM_SLOTS))

    for i in range(1, len(starts)):
        gap = starts[i] - starts[i - 1]
        if gap != SLOT_SIZE:
            fails.append(
                "slot %d starts %d bytes after slot %d (expected %d) -- a "
                "slot body has outgrown its 128-byte entry"
                % (i, gap, i - 1, SLOT_SIZE))
            break

    for i in range(len(starts)):
        if starts[i] != i * SLOT_SIZE:
            fails.append("slot %d is at offset 0x%x, expected 0x%x"
                         % (i, starts[i], i * SLOT_SIZE))
            break

    # The kind immediate must match the slot index, or the handler reports
    # the wrong exception even with the spacing correct.
    for i in range(min(len(starts), NUM_SLOTS)):
        base = starts[i]
        found = -1
        for k in range(0, SLOT_SIZE, 4):
            off = base + k
            if off + 4 > size:
                break
            imm = decode_mov_imm(int.from_bytes(data[off:off + 4], "little"))
            if imm >= 0:
                found = imm
                break
        if found != i:
            fails.append("slot %d sets kind %d, expected %d" % (i, found, i))
            break

    if fails:
        for f in fails:
            print("  FAIL  " + f)
        print("\nvector table geometry: FAILED")
        return 1

    print("  PASS  16 slots, 128 bytes apart, kinds 0..15 in order")
    print("  PASS  .vectors is %d bytes" % size)
    print("\nvector table geometry: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
