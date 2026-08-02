"""Differential test for the ShivyCX-built mingine kernel.

One scene (`examples/crust/baremetalgames/scene.c`) is compiled twice by
ShivyCX and run in two places:

  * hosted, as an ordinary process (`scene_host.c`)
  * on bare metal, as a bootable image (`examples/baremetal/kernel_mingine.c`)

Both print the same five lines. If they match, the identical source -- C, plus
Rust spliced in by `shivyc/crust.py` from `mingine.rs`, plus rpython lowered by
`py2c.py` from `mingine.py` -- produced bit-identical results in a process and
on the metal, through a compiler that is on its way to compiling itself.

The checksum line is what makes this worth running. "It booted and something
appeared on screen" is compatible with a great many bugs; "every pixel of the
final frame hashes to the same value in both worlds" is not. The sprite
positions are printed alongside it so that a future mismatch says immediately
whether the *simulation* diverged or only the *rasterisation*.

The screenshot check is deliberately weak -- a handful of colours the scene is
known to contain. It exists to catch the case where the render is right and the
blit is not, which the checksum cannot see.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
KERNEL = os.path.join(HERE, "kernel_mingine.c")
HOST_C = os.path.join(REPO, "examples", "crust", "baremetalgames",
                      "scene_host.c")
BUILD = os.path.join(REPO, "build")
IMAGE = os.path.join(BUILD, "mingine.elf")
HOST_BIN = os.path.join(BUILD, "scene_host")
PY = os.environ.get("SHIVYCX_PY", sys.executable)
QEMU = "qemu-system-x86_64"
MON_PORT = int(os.environ.get("MBOS_MON_PORT", "55771"))

passed, failures = [], []


def check(desc, ok, detail=""):
    (passed if ok else failures).append(
        desc + ((" -- " + detail) if not ok and detail else ""))


def summary_lines(text):
    """The deterministic part of either side's output.

    Both sides print other things -- the kernel logs its boot and blit -- so
    compare only the lines the scene itself produces.
    """
    keys = ("scene ", "ball ", "foe ", "score ", "pixels ")
    out = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if line.startswith(keys):
            out.append(line)
    return out


def build_hosted():
    r = subprocess.run([PY, "-m", "shivyc.main", HOST_C, "-o", HOST_BIN],
                       cwd=REPO, capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(HOST_BIN), r


def build_image():
    r = subprocess.run([PY, os.path.join(REPO, "shivycx_baremetal.py"),
                        KERNEL, "-o", IMAGE, "--image"],
                       cwd=REPO, capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(IMAGE), r


def boot(shot=None, run_secs=32):
    """Boot the image, capture serial, optionally screendump."""
    log = tempfile.NamedTemporaryFile(prefix="mingine_", delete=False)
    log.close()
    args = [QEMU, "-kernel", IMAGE, "-no-reboot", "-display", "none",
            "-vga", "std", "-serial", "file:" + log.name]
    if shot:
        args += ["-monitor", "tcp:127.0.0.1:%d,server,nowait" % MON_PORT]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        time.sleep(run_secs)
        if shot:
            try:
                sock = socket.create_connection(("127.0.0.1", MON_PORT),
                                                timeout=5)
                time.sleep(0.4)
                sock.sendall(("screendump " + shot + "\n").encode())
                time.sleep(3.0)
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


def read_ppm_colours(path):
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(b"P6"):
        return None
    fields, i = [], 2
    while len(fields) < 3:
        while i < len(data) and data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b"#":
            while i < len(data) and data[i:i + 1] != b"\n":
                i += 1
            continue
        j = i
        while j < len(data) and not data[j:j + 1].isspace():
            j += 1
        fields.append(int(data[i:j]))
        i = j
    i += 1
    body = data[i:]
    seen = set()
    for o in range(0, len(body) - 2, 3):
        seen.add((body[o], body[o + 1], body[o + 2]))
    return seen


def main():
    if not os.path.isdir(BUILD):
        os.makedirs(BUILD)

    ok, r = build_hosted()
    check("ShivyCX compiles the scene hosted", ok,
          (r.stdout or "")[-400:] + (r.stderr or "")[-400:])
    if not ok:
        report()

    ok, r = build_image()
    check("ShivyCX builds a bootable image from the same scene", ok,
          (r.stdout or "")[-400:] + (r.stderr or "")[-400:])
    if not ok:
        report()

    # The image must be entirely ShivyCX's own output. If a minikraft piece
    # gets linked in, part of the kernel came from gcc and the claim weakens.
    check("image needs no OS pieces (all of it is ShivyCX output)",
          "OS pieces linked in: (none)" in (r.stdout or ""),
          "linked: " + (r.stdout or "").split("OS pieces linked in:")[-1].strip()
          if "OS pieces linked in:" in (r.stdout or "") else "unknown")

    host_out = subprocess.run([HOST_BIN], capture_output=True, text=True,
                              timeout=120).stdout
    host = summary_lines(host_out)
    check("hosted run produced a summary", len(host) == 5,
          "got %d line(s): %r" % (len(host), host))

    shot = os.path.join(BUILD, "mingine_probe.ppm")
    if os.path.exists(shot):
        os.unlink(shot)
    bm_out = boot(shot=shot)
    bm = summary_lines(bm_out)

    check("kernel booted", "[mingine] booted" in bm_out,
          "no boot banner; qemu may have rejected the image")
    check("kernel ran to completion", "[mingine] done." in bm_out)
    check("bare-metal run produced a summary", len(bm) == 5,
          "got %d line(s): %r" % (len(bm), bm))

    if len(host) == 5 and len(bm) == 5:
        for h, b in zip(host, bm):
            field = h.split()[0]
            check("hosted and bare metal agree on '%s'" % field, h == b,
                  "hosted %r vs bare metal %r" % (h, b))

    check("framebuffer was brought up and blitted",
          "[mingine] blitted to framebuffer" in bm_out,
          "no blit; the LFB may be outside the identity map")

    if os.path.exists(shot):
        colours = read_ppm_colours(shot)
        # Colours the scene is known to contain, straight from scene.c.
        want = {(18, 42, 24): "ground", (210, 120, 90): "bricks",
                (255, 220, 120): "sun"}
        missing = [n for c, n in want.items() if c not in colours]
        check("scene pixels reached the screen", not missing,
              "missing: %s" % ", ".join(missing))
        check("screen is not a flat fill", len(colours) > 40,
              "only %d distinct colours" % len(colours))
        os.unlink(shot)
    else:
        check("screenshot captured", False, "no screendump produced")

    report()


def report():
    for p in passed:
        print("  ok    " + p)
    print()
    if failures:
        for f in failures:
            print("FAIL -- " + f)
        sys.exit(1)
    print("PASS -- ShivyCX built the same scene hosted and bare metal, "
          "bit-identical.")
    sys.exit(0)


if __name__ == "__main__":
    main()
