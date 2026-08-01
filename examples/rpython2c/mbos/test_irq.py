"""Headless self-test for the interrupt path.

test.py proves the kernel renders. This one proves it *responds*. There is no
way to check that from the serial line alone, because the serial port is
output-only here -- the keyboard is a real PS/2 device on IRQ1. So we drive it
the way a user would, through QEMU's monitor `sendkey`, and watch what comes
back on serial.

Three things are being established:

  1. IRQ0 fires. The `ticks` command reports a counter that only the timer
     handler increments, so a non-zero value means the PIT is wired, the PIC
     is remapped, and the EOI path works.
  2. IRQ1 fires and translates. Typing `help` and seeing the shell dispatch it
     means scancode set 1 translation, the ring buffer, and the handoff to the
     foreground loop all work.
  3. Nothing faults. A general protection fault or a bad IDT entry would show
     up as the PANIC banner from irq.c, so we assert it never appears.
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

# `sendkey` wants QEMU key names, not characters.
KEYNAME = {
    " ": "spc", "\n": "ret", "-": "minus", ".": "dot", "/": "slash",
}


def keys_for(text):
    for ch in text:
        yield KEYNAME.get(ch, ch)


def monitor_cmd(sock, cmd, settle=0.06):
    sock.sendall((cmd + "\n").encode())
    time.sleep(settle)


def run():
    if not os.path.exists(ELF):
        sys.exit("missing %s -- run `make` first" % ELF)

    serial = tempfile.NamedTemporaryFile(prefix="mbos_irq_", suffix=".log",
                                         delete=False)
    serial.close()
    mon_port = 55555

    proc = subprocess.Popen(
        [QEMU, "-kernel", ELF, "-no-reboot", "-vga", "std",
         "-display", "none",
         "-serial", "file:" + serial.name,
         "-monitor", "tcp:127.0.0.1:%d,server,nowait" % mon_port],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        # Give the kernel time to boot, render, and reach the prompt.
        time.sleep(3.0)

        sock = socket.create_connection(("127.0.0.1", mon_port), timeout=5)
        time.sleep(0.3)

        # Let the timer run a while so `ticks` has something to report.
        time.sleep(1.5)

        for word in ("help", "ticks"):
            for key in keys_for(word):
                monitor_cmd(sock, "sendkey " + key)
            monitor_cmd(sock, "sendkey ret", settle=0.4)

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

    failures = []

    if "[mbos] interrupts live" not in out:
        failures.append("kernel never reported interrupts live")
    else:
        print("  ok    irq_init completed (IDT loaded, sti issued)")

    if "PANIC" in out:
        banner = out[out.index("PANIC"):][:400]
        failures.append("kernel faulted:\n" + banner)
    else:
        print("  ok    no CPU exception during the interrupt run")

    # IRQ1: the typed command came through and was dispatched. The shell logs
    # every successful lookup, so seeing the exec line means the whole chain --
    # scancode, translation, ring, editor, tokenize, table lookup -- ran.
    if "[mbos] exec help" in out:
        print("  ok    IRQ1 keyboard -- 'help' typed, translated, dispatched")
    else:
        failures.append("typed 'help' never reached the command handler")

    # IRQ0: a non-zero tick count means the PIT handler ran. `ticks` prints the
    # bare number on the line after its exec marker.
    m = re.search(r"exec ticks\s+(\d+)", out)
    if not m:
        failures.append("'ticks' command produced no output")
    elif int(m.group(1)) == 0:
        failures.append("tick counter is still zero -- IRQ0 is not firing")
    else:
        n = int(m.group(1))
        print("  ok    IRQ0 timer   -- %d ticks accumulated (~%.1fs at 100Hz)"
              % (n, n / 100.0))

    print()
    if failures:
        for f in failures:
            print("FAIL -- " + f)
        print("\n---- serial ----\n" + out[-1500:])
        sys.exit(1)

    print("PASS -- interrupt path live: IDT, PIC, PIT, PS/2 keyboard.")


if __name__ == "__main__":
    run()
