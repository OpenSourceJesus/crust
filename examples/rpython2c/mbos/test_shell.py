"""Headless self-test for the mbos shell.

test_irq.py established that keystrokes reach the kernel. This one checks what
the shell does with them, driving the guest through QEMU's monitor exactly as a
user would and reading the results off the serial mirror.

The interesting cases are the ones where the editor has to do something other
than append:

  * `echo` proves tokenize() produces a real argv, not just a command name.
  * Typing a word, then Left Left, then inserting, proves cursor movement and
    mid-line insert repaint the line correctly -- the command only resolves if
    the buffer really holds what the screen shows.
  * Backspacing a mistyped command into a correct one proves delete works at
    the end of the line.
  * Up-arrow proves the history ring returns the previous entry.
  * An unknown command proves the not-found path is reached rather than
    silently ignored.
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
MON_PORT = int(os.environ.get("MBOS_MON_PORT", "55571"))

KEYNAME = {
    " ": "spc", "\n": "ret", "-": "minus", ".": "dot", "/": "slash",
    ",": "comma", ";": "semicolon", "=": "equal", "'": "apostrophe",
}


class Guest(object):
    def __init__(self, sock):
        self.sock = sock

    def key(self, name, settle=0.05):
        self.sock.sendall(("sendkey " + name + "\n").encode())
        time.sleep(settle)

    def type(self, text):
        for ch in text:
            self.key(KEYNAME.get(ch, ch))

    def enter(self):
        self.key("ret", settle=0.45)

    def line(self, text):
        self.type(text)
        self.enter()


def drive(g):
    # Give the shell a moment after the banner.
    time.sleep(0.4)

    # 1. plain dispatch + argv
    g.line("echo hello world")

    # 2. mid-line insert: "eco x", three lefts to sit after the c, insert "h"
    g.type("eco x")
    for _ in range(3):
        g.key("left")
    g.type("h")
    g.enter()

    # 3. backspace: type "verx", rub out the x -> "ver"
    g.type("verx")
    g.key("backspace")
    g.enter()

    # 4. history: up-arrow should bring back "ver"
    g.key("up", settle=0.2)
    g.enter()

    # 5. unknown command
    g.line("frobnicate")

    # 6. a command with numeric output, to confirm the table is not just echo
    g.line("uptime")

    time.sleep(0.6)


def run():
    if not os.path.exists(ELF):
        sys.exit("missing %s -- run `make` first" % ELF)

    serial = tempfile.NamedTemporaryFile(prefix="mbos_shell_", suffix=".log",
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
        time.sleep(0.3)
        drive(Guest(sock))
        sock.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    out = open(serial.name, "r", errors="replace").read()
    os.unlink(serial.name)

    execs = re.findall(r"\[mbos\] exec (\w+)", out)
    passed, failures = [], []

    def check(desc, ok, detail=""):
        if ok:
            passed.append(desc)
        else:
            failures.append(desc + ((" -- " + detail) if detail else ""))

    check("shell reached the prompt", "[mbos] shell ready" in out)
    check("prompt reads 'crust>'", "crust> " in out)
    check("no CPU exception during the session", "PANIC" not in out)

    check("dispatch: 'echo hello world' ran with argv",
          "hello world" in out and "echo" in execs)

    # "eco x" + left*4 + "h" -> "echo x". If the insert landed anywhere else the
    # command name would not resolve.
    check("mid-line insert: left-arrow + type produced 'echo'",
          execs.count("echo") >= 2,
          "echo ran %d time(s), expected 2" % execs.count("echo"))

    check("backspace: 'verx' rubbed down to 'ver'", "ver" in execs)

    check("history: up-arrow replayed the previous command",
          execs.count("ver") >= 2,
          "ver ran %d time(s), expected 2" % execs.count("ver"))

    check("unknown command reported, not ignored",
          "[mbos] not found: frobnicate" in out)

    m = re.search(r"exec uptime", out)
    check("table dispatch reached 'uptime'", m is not None)

    for p in passed:
        print("  ok    " + p)
    print()
    if failures:
        for f in failures:
            print("FAIL -- " + f)
        print("\nexecs seen: %r" % (execs,))
        print("\n---- serial tail ----\n" + out[-1800:])
        sys.exit(1)

    print("PASS -- shell live: dispatch, argv, editing keys, history.")


if __name__ == "__main__":
    run()
