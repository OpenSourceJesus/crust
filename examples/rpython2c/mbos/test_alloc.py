"""Headless self-test for the kernel heap.

Drives `mem` and `memtest` from the shell through QEMU's monitor. What matters
is not that allocation returns a pointer -- almost any bump allocator does that
-- but that the block list stays a valid covering of the arena across a churn
of allocations and interleaved frees, and that it collapses back to a single
free block once everything is released. A missed coalesce leaves the heap
usable but progressively more fragmented, which is the failure that does not
announce itself.

`mem check` runs the invariant audit that lives in alloc.rs: blocks in address
order, starting at zero, no gaps, no two adjacent free blocks, summing to the
arena size.
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ELF = os.environ.get("MBOS_ELF", os.path.join(HERE, "build", "mbos.elf"))
QEMU = "qemu-system-x86_64"
MON_PORT = int(os.environ.get("MBOS_MON_PORT", "55601"))

KEYNAME = {" ": "spc", "\n": "ret", "-": "minus", ".": "dot", "/": "slash"}


def run():
    if not os.path.exists(ELF):
        sys.exit("missing %s -- run `make` first" % ELF)

    serial = tempfile.NamedTemporaryFile(prefix="mbos_alloc_", suffix=".log",
                                         delete=False)
    serial.close()

    proc = subprocess.Popen(
        [QEMU, "-kernel", ELF, "-no-reboot", "-vga", "std",
         "-display", "none",
         "-serial", "file:" + serial.name,
         "-monitor", "tcp:127.0.0.1:%d,server,nowait" % MON_PORT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        time.sleep(3.0)
        sock = socket.create_connection(("127.0.0.1", MON_PORT), timeout=5)
        time.sleep(0.4)

        def key(name, settle=0.05):
            sock.sendall(("sendkey " + name + "\n").encode())
            time.sleep(settle)

        def line(text, settle=0.6):
            for ch in text:
                key(KEYNAME.get(ch, ch))
            key("ret", settle=settle)

        line("mem")            # baseline: untouched arena
        line("mem check")
        line("memtest")        # churn: 16 allocs, interleaved frees
        line("mem check")      # still a valid covering afterwards
        line("mem")            # and back to the baseline shape

        time.sleep(0.5)
        sock.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    out = open(serial.name, "r", errors="replace").read()
    os.unlink(serial.name)

    passed, failures = [], []

    def check(desc, ok, detail=""):
        (passed if ok else failures).append(
            desc + ((" -- " + detail) if not ok and detail else ""))

    check("no CPU exception during heap churn", "PANIC" not in out)

    blocks = [int(m) for m in re.findall(r"blocks\s+(\d+)", out)]
    used_pairs = re.findall(r"used\s+(\d+) / (\d+)", out)

    check("`mem` reported a heap", len(used_pairs) >= 2,
          "saw %d summaries" % len(used_pairs))

    if used_pairs:
        total = int(used_pairs[0][1])
        check("arena is the configured 1 MiB", total == 1024 * 1024,
              "total=%d" % total)
        check("heap starts empty", int(used_pairs[0][0]) == 0,
              "used=%s at boot" % used_pairs[0][0])

    if blocks:
        check("untouched arena is a single block", blocks[0] == 1,
              "blocks=%d" % blocks[0])

    consistent = out.count("heap consistent")
    check("invariant held before and after churn", consistent >= 2,
          "%d of 2 checks passed" % consistent)
    check("invariant never reported broken", "INCONSISTENT" not in out)

    check("alloc/free churn survived and fully coalesced",
          "[mbos] memtest ok" in out,
          "memtest did not report ok")

    if len(used_pairs) >= 2:
        check("heap returned to empty after all frees",
              int(used_pairs[-1][0]) == 0,
              "used=%s at the end" % used_pairs[-1][0])
    if len(blocks) >= 2:
        check("block list collapsed back to one",
              blocks[-1] == 1, "blocks=%d at the end" % blocks[-1])

    # memtest deliberately double-frees and passes a pointer outside the arena.
    # Both must be *reported and refused*, not silently accepted -- accepting a
    # double free would merge two live blocks and corrupt the heap.
    check("double free was detected and refused",
          "not a live block" in out)
    check("out-of-arena pointer was detected and refused",
          "outside the arena" in out)
    check("neither rejection broke the invariant",
          "memtest FAIL" not in out)

    for p in passed:
        print("  ok    " + p)
    print()
    if failures:
        for f in failures:
            print("FAIL -- " + f)
        print("\n---- serial tail ----\n" + out[-2000:])
        sys.exit(1)

    print("PASS -- heap live: alloc, split, free, coalesce, invariant holds.")


if __name__ == "__main__":
    run()
