"""Cross-compiler test for the mingine scene.

The same source -- `examples/crust/baremetalgames/scene.c`, which pulls in
`mingine.c` (C), `mingine.rs` (Rust) and `mingine.py` (rpython) -- is built
three ways and required to produce the identical picture:

  1. **hosted**, by ShivyCX, as an ordinary process
  2. **bare metal**, by ShivyCX, as its own bootable image
  3. **inside mbos**, by gcc, with the Rust and rpython halves pre-lowered by
     `gen_mingine.py` and the result driven from the shell's `demo` command

The third is what this file adds. `examples/baremetal/test_mingine.py` already
covers the first two, and those share a compiler; bringing gcc in makes the
comparison meaningful in a different way. If ShivyCX ever miscompiles the
engine -- a wrong shift, a sign-extension, an off-by-one in the Rust splice --
agreeing with itself in two places would not reveal it. Agreeing with gcc
would be a coincidence.

mg_checksum() is a 32-bit hash of the whole final frame, so a single wrong
pixel changes it. Sprite positions are compared too, so a future mismatch says
whether the simulation diverged or only the rasterisation.
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
ELF = os.path.join(HERE, "build", "mbos.elf")
INITRD = os.path.join(HERE, "build", "initrd.tar")
HOST_C = os.path.join(REPO, "examples", "crust", "baremetalgames",
                      "scene_host.c")
HOST_BIN = os.path.join(REPO, "build", "scene_host")
PY = os.environ.get("SHIVYCX_PY", sys.executable)
QEMU = "qemu-system-x86_64"
MON_PORT = int(os.environ.get("MBOS_MON_PORT", "55811"))

passed, failures = [], []


def check(desc, ok, detail=""):
    (passed if ok else failures).append(
        desc + ((" -- " + detail) if not ok and detail else ""))


def run_mbos_demo(shot=None):
    log = tempfile.NamedTemporaryFile(prefix="mbos_demo_", delete=False)
    log.close()
    proc = subprocess.Popen(
        [QEMU, "-kernel", ELF, "-no-reboot", "-display", "none", "-vga", "std",
         "-serial", "file:" + log.name, "-initrd", INITRD,
         "-monitor", "tcp:127.0.0.1:%d,server,nowait" % MON_PORT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3.5)
        sock = socket.create_connection(("127.0.0.1", MON_PORT), timeout=5)
        time.sleep(0.4)
        for ch in "demo":
            sock.sendall(("sendkey " + ch + "\n").encode())
            time.sleep(0.05)
        sock.sendall(b"sendkey ret\n")
        time.sleep(7.0)
        if shot:
            sock.sendall(("screendump " + shot + "\n").encode())
            time.sleep(2.5)
        sock.close()
    except OSError:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
    out = open(log.name, errors="replace").read()
    os.unlink(log.name)
    return out


def parse_hosted(text):
    """The hosted runner prints the checksum in hex; mbos prints decimal."""
    got = {}
    for line in text.replace("\r", "").split("\n"):
        f = line.strip().split()
        if len(f) == 2 and f[0] == "pixels":
            got["pixels"] = int(f[1], 16)
        elif len(f) == 2 and f[0] in ("ball", "foe"):
            got[f[0]] = f[1]
        elif len(f) == 2 and f[0] == "score":
            got["score"] = int(f[1])
    return got


def parse_mbos(text):
    m = re.search(r"\[mbos\] demo pixels (\d+) ball (\S+) foe (\S+) score (\d+)",
                  text)
    if not m:
        return {}
    return {"pixels": int(m.group(1)), "ball": m.group(2),
            "foe": m.group(3), "score": int(m.group(4))}


def colours_in(path):
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(b"P6"):
        return set()
    fields, i = [], 2
    while len(fields) < 3:
        while i < len(data) and data[i:i + 1].isspace():
            i += 1
        j = i
        while j < len(data) and not data[j:j + 1].isspace():
            j += 1
        fields.append(int(data[i:j]))
        i = j
    i += 1
    body = data[i:]
    return {(body[o], body[o + 1], body[o + 2])
            for o in range(0, len(body) - 2, 3)}


def main():
    for p in (ELF, INITRD):
        if not os.path.exists(p):
            sys.exit("missing %s -- run `make` first" % p)

    # Reference: the same scene compiled hosted by ShivyCX.
    r = subprocess.run([PY, "-m", "shivyc.main", HOST_C, "-o", HOST_BIN],
                       cwd=REPO, capture_output=True, text=True)
    check("ShivyCX compiles the reference scene hosted",
          r.returncode == 0 and os.path.exists(HOST_BIN),
          (r.stdout or "")[-300:] + (r.stderr or "")[-300:])
    if failures:
        report()

    host = parse_hosted(subprocess.run([HOST_BIN], capture_output=True,
                                       text=True, timeout=120).stdout)
    check("hosted reference produced a summary", len(host) == 4, repr(host))

    shot = os.path.join(HERE, "build", "demo_probe.ppm")
    if os.path.exists(shot):
        os.unlink(shot)
    out = run_mbos_demo(shot=shot)

    check("no CPU exception while running the demo", "PANIC" not in out)
    check("mbos dispatched the demo command", "[mbos] exec demo" in out)

    mb = parse_mbos(out)
    check("mbos demo produced a summary", len(mb) == 4,
          "serial tail: " + out[-300:])

    if len(host) == 4 and len(mb) == 4:
        check("gcc-built mbos and ShivyCX agree on the pixel checksum",
              mb["pixels"] == host["pixels"],
              "mbos 0x%08x vs ShivyCX 0x%08x" % (mb["pixels"], host["pixels"]))
        check("...and on the ball position",
              mb["ball"] == host["ball"],
              "%s vs %s" % (mb["ball"], host["ball"]))
        check("...and on the foe position",
              mb["foe"] == host["foe"],
              "%s vs %s" % (mb["foe"], host["foe"]))
        check("...and on the score",
              mb["score"] == host["score"],
              "%d vs %d" % (mb["score"], host["score"]))

    # The checksum covers the RAM buffer; this covers the blit out of it.
    if os.path.exists(shot):
        cols = colours_in(shot)
        want = {(18, 42, 24): "ground", (210, 120, 90): "bricks",
                (255, 220, 120): "sun", (102, 76, 136): "pyramid"}
        missing = [n for c, n in want.items() if c not in cols]
        check("scene reached the mbos framebuffer", not missing,
              "missing: " + ", ".join(missing))
        check("screen is not a flat fill", len(cols) > 40,
              "%d distinct colours" % len(cols))
        os.unlink(shot)
    else:
        check("screenshot captured", False, "no screendump")

    report()


def report():
    for p in passed:
        print("  ok    " + p)
    print()
    if failures:
        for f in failures:
            print("FAIL -- " + f)
        sys.exit(1)
    print("PASS -- gcc-built mbos and ShivyCX render the same scene "
          "bit-identically.")
    sys.exit(0)


if __name__ == "__main__":
    main()
