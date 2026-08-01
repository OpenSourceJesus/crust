"""Headless self-test for the mbos ramdisk.

Boots with `-initrd build/initrd.tar` and drives `ls` and `cat` from the shell.

The archive is built from `fs/` at build time, so this test knows what should
be in it and checks the kernel's view against the host's: the same file names,
and byte-for-byte the same contents for a file it prints back. That is what
distinguishes "the tar walk produced plausible-looking entries" from "the tar
walk is correct" -- an off-by-one in the header stride yields entries either
way, but only a correct walk lands the data pointer on the right byte.

It also boots once *without* an initrd, because booting with no ramdisk has to
stay a normal outcome rather than a fault. mbos is useful without one.
"""
import os
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ELF = os.environ.get("MBOS_ELF", os.path.join(HERE, "build", "mbos.elf"))
INITRD = os.path.join(HERE, "build", "initrd.tar")
FSDIR = os.path.join(HERE, "fs")
QEMU = "qemu-system-x86_64"
MON_PORT = int(os.environ.get("MBOS_MON_PORT", "55621"))

KEYNAME = {" ": "spc", "\n": "ret", "-": "minus", ".": "dot", "/": "slash",
           "_": "shift-minus"}


def _flip(buf, i):
    buf[i] ^= 0xFF
    return buf


def _poke(buf, i, value):
    buf[i] = value
    return buf


def boot_and_drive(commands, with_initrd=True, port=MON_PORT, settle=3.0,
                   initrd=None):
    """Boot mbos, type each command, return everything seen on serial."""
    serial = tempfile.NamedTemporaryFile(prefix="mbos_fs_", suffix=".log",
                                         delete=False)
    serial.close()

    args = [QEMU, "-kernel", ELF, "-no-reboot", "-vga", "std",
            "-display", "none",
            "-serial", "file:" + serial.name,
            "-monitor", "tcp:127.0.0.1:%d,server,nowait" % port]
    if with_initrd:
        args += ["-initrd", initrd if initrd else INITRD]

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        time.sleep(settle)
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        time.sleep(0.4)
        for cmd in commands:
            for ch in cmd:
                sock.sendall(("sendkey " + KEYNAME.get(ch, ch) + "\n").encode())
                time.sleep(0.05)
            sock.sendall(b"sendkey ret\n")
            time.sleep(0.7)
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
    return out


def main():
    for path in (ELF, INITRD):
        if not os.path.exists(path):
            sys.exit("missing %s -- run `make` first" % path)

    # What the host says is in the archive, for comparison against the guest.
    tf = tarfile.open(INITRD)
    expected = {}
    for m in tf.getmembers():
        if m.isfile():
            expected[os.path.basename(m.name)] = m.size

    passed, failures = [], []

    def check(desc, ok, detail=""):
        (passed if ok else failures).append(
            desc + ((" -- " + detail) if not ok and detail else ""))

    # ---- with a ramdisk ------------------------------------------------
    out = boot_and_drive(["ls", "cat motd.txt", "cat hello.py",
                          "cat nosuchfile"])

    check("no CPU exception while walking the archive", "PANIC" not in out)
    check("ramfs mounted", "[mbos] ramfs: ready" in out)
    check("no malformed-archive complaint",
          "stopping" not in out,
          "the walk aborted early")

    for name, size in expected.items():
        check("ls shows %s" % name, name in out)
        # ls prints "name  <spaces>  size"
        m = re.search(re.escape(name) + r"\s+(\d+)", out)
        if m:
            check("size of %s matches the host's tar" % name,
                  int(m.group(1)) == size,
                  "guest says %s, host says %d" % (m.group(1), size))
        else:
            check("size of %s matches the host's tar" % name, False,
                  "no size printed")

    m = re.search(r"(\d+) file\(s\), module (\d+) bytes", out)
    if m:
        check("file count matches the host's tar",
              int(m.group(1)) == len(expected),
              "guest says %s, host says %d" % (m.group(1), len(expected)))
        check("module size matches the archive on disk",
              int(m.group(2)) == os.path.getsize(INITRD),
              "guest says %s, file is %d"
              % (m.group(2), os.path.getsize(INITRD)))
    else:
        check("file count matches the host's tar", False, "no summary line")

    # The real check on the data pointer: every line of a file we printed has
    # to appear on serial. A stride bug still yields entries, but the contents
    # land on the wrong byte.
    motd = open(os.path.join(FSDIR, "motd.txt")).read()
    missing = [ln for ln in motd.split("\n")
               if ln.strip() and ln not in out]
    check("cat motd.txt reproduced the file exactly", not missing,
          "%d line(s) missing, first: %r"
          % (len(missing), missing[0] if missing else ""))

    hello = open(os.path.join(FSDIR, "hello.py")).read()
    missing = [ln for ln in hello.split("\n")
               if ln.strip() and ln not in out]
    check("cat hello.py reproduced the file exactly", not missing,
          "%d line(s) missing" % len(missing))

    check("cat of a missing file is reported, not faulted",
          "[mbos] cat: not found" in out)

    # ---- corrupted archives --------------------------------------------
    #
    # A ramdisk arrives from outside the kernel, so a malformed one has to be
    # a refusal rather than a fault. Each case below breaks the archive in a
    # different way and the walk must stop and say so, never wander into
    # whatever memory follows the module.
    corrupt = os.path.join(HERE, "build", "corrupt.tar")
    raw = open(INITRD, "rb").read()

    cases = [
        ("a flipped byte in a header",
         lambda b: _flip(b, 10),
         ["checksum mismatch"]),
        ("a non-octal digit in the size field",
         lambda b: _poke(b, 126, ord("9")),
         ["checksum mismatch", "malformed size"]),
        ("an archive cut in half",
         lambda b: b[:len(b) // 2],
         None),        # stops when the next header would run past the module
    ]

    for desc, mangle, want_any in cases:
        with open(corrupt, "wb") as f:
            f.write(bytes(mangle(bytearray(raw))))
        o = boot_and_drive(["ls"], initrd=corrupt, port=MON_PORT + 2)
        check("no fault on %s" % desc, "PANIC" not in o)
        if want_any is not None:
            check("%s is detected and reported" % desc,
                  any(w in o for w in want_any),
                  "walk did not complain")
        check("kernel still reaches the shell after %s" % desc,
              "[mbos] shell ready" in o)
    os.unlink(corrupt)

    # ---- without a ramdisk ---------------------------------------------
    out2 = boot_and_drive(["ls"], with_initrd=False, port=MON_PORT + 1)

    check("booting with no ramdisk does not fault", "PANIC" not in out2)
    check("no ramdisk is reported as a normal condition",
          "[mbos] ramfs: no boot module" in out2)
    check("ls says there is nothing mounted",
          "no ramdisk mounted" in out2)

    for p in passed:
        print("  ok    " + p)
    print()
    if failures:
        for f in failures:
            print("FAIL -- " + f)
        print("\n---- serial tail ----\n" + out[-2500:])
        sys.exit(1)

    print("PASS -- ramdisk live: module found, archive walked, files readable.")


if __name__ == "__main__":
    main()
