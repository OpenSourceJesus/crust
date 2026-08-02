"""Headless self-test for hi-res framebuffer support.

Boots the same kernel against several QEMU display configurations and checks
what the driver makes of each. The cases that matter are the ones where the
device and the request disagree.

  * **Geometry limits and memory limits are different limits.** QEMU's std VGA
    reports a maximum of 16000x12000 but ships 16 MiB of video memory, which is
    enough for 1920x1080 (7.9 MiB) and not for 3840x2160 (31.6 MiB). A driver
    that only checks the geometry accepts the second, sets the mode, and then
    draws off the end of VRAM. The refusal is the interesting behaviour, so it
    is tested directly.

  * **Stride is not width.** The scanline stride is the device's *virtual*
    width, which it may round up. `gfxtest` puts markers in all four corners;
    if the stride were wrong the right-hand markers would walk diagonally down
    the screen instead of sitting on the edge. Reading them back out of a
    screenshot is what turns that from a thing you would notice eventually into
    a thing the test notices.

  * **Falling back is not failing.** With no usable graphics mode the kernel
    has to come up in VGA text and still reach a shell.
"""
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ELF = os.path.join(HERE, "build", "mbos.elf")
HELF = os.path.join(HERE, "build", "mbos_hires.elf")
K4ELF = os.path.join(HERE, "build", "mbos_4k.elf")
QEMU = "qemu-system-x86_64"

STD = ["-vga", "std"]                                    # 16 MiB
BIG = ["-vga", "none", "-device", "VGA,vgamem_mb=64"]    # 64 MiB

passed, failures = [], []


def check(desc, ok, detail=""):
    (passed if ok else failures).append(
        desc + ((" -- " + detail) if not ok and detail else ""))


def boot(elf, vga, port, cmds, shot=None, settle=3.5):
    """Boot, type commands, optionally screendump; return serial output."""
    log = tempfile.NamedTemporaryFile(prefix="mbos_gfx_", delete=False)
    log.close()
    proc = subprocess.Popen(
        [QEMU, "-kernel", elf, "-no-reboot", "-display", "none",
         "-serial", "file:" + log.name,
         "-monitor", "tcp:127.0.0.1:%d,server,nowait" % port] + vga,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(settle)
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        time.sleep(0.3)
        for cmd in cmds:
            for ch in cmd:
                sock.sendall(("sendkey " + {" ": "spc"}.get(ch, ch) + "\n")
                             .encode())
                time.sleep(0.05)
            sock.sendall(b"sendkey ret\n")
            time.sleep(1.0)
        if shot:
            sock.sendall(("screendump " + shot + "\n").encode())
            time.sleep(2.0)
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


def read_ppm(path):
    """Minimal binary-PPM reader: returns (w, h, pixel_getter)."""
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
    w, h, _maxval = fields
    body = data[i:]

    def px(x, y):
        o = (y * w + x) * 3
        return (body[o], body[o + 1], body[o + 2])

    return w, h, px


def near(got, want, tol=24):
    return all(abs(a - b) <= tol for a, b in zip(got, want))


def main():
    for p in (ELF, HELF, K4ELF):
        if not os.path.exists(p):
            sys.exit("missing %s -- `make test-gfx` builds all three" % p)

    # ---- 1024x768 on the 16 MiB std device -----------------------------
    out = boot(ELF, STD, 55741, ["gfx"])
    check("std VGA: framebuffer comes up", "[gfx] framebuffer up" in out)
    check("std VGA: mode is 1024x768",
          "[mbos] gfx 1024x768" in out)
    check("std VGA: device reports its video memory",
          "vram 16777216" in out, "no vram figure on serial")
    check("std VGA: device reports a geometry maximum",
          "max 16000x12000" in out)
    check("std VGA: stride reported and equals width",
          "stride 1024" in out)

    # ---- 1920x1080 -----------------------------------------------------
    # 7.9 MiB, so it fits the 16 MiB std device too -- the README's note that
    # hi-res *needs* vgamem_mb=64 is true only past roughly 2560x1600.
    out = boot(HELF, BIG, 55742, ["gfx"])
    check("64 MiB: 1920x1080 comes up", "[mbos] gfx 1920x1080" in out)
    out = boot(HELF, STD, 55743, ["gfx"])
    check("16 MiB: 1920x1080 also fits (7.9 MiB)",
          "[mbos] gfx 1920x1080" in out,
          "refused a mode that fits")

    # ---- 3840x2160: the mode-negotiation case ---------------------------
    # 31.6 MiB of framebuffer. Fits 64 MiB, does not fit 16 MiB. The device
    # would accept it either way -- 3840x2160 is well inside its stated
    # 16000x12000 -- so the memory check is the only thing standing between a
    # 16 MiB guest and a display scanning out unmapped VRAM.
    out = boot(K4ELF, BIG, 55745, ["gfx"], settle=4.0)
    check("64 MiB: 3840x2160 comes up", "[mbos] gfx 3840x2160" in out)
    check("64 MiB: 4K stride reported", "stride 3840" in out)

    out = boot(K4ELF, STD, 55746, ["gfx"], settle=4.0)
    check("16 MiB: 4K is refused, not set",
          "requested mode exceeds the device" in out,
          "driver accepted a mode 2x larger than VRAM")
    check("16 MiB: refusal names the actual limit",
          "needs 31 MiB, device has 16 MiB" in out)
    check("16 MiB: falls back to VGA text rather than failing",
          "[con] text console" in out)
    check("16 MiB: still reaches a usable shell after falling back",
          "no framebuffer (vga text mode)" in out,
          "shell did not respond after the fallback")

    # ---- stride correctness, read back off the screen -------------------
    shot = os.path.join(HERE, "build", "gfx_probe.ppm")
    if os.path.exists(shot):
        os.unlink(shot)
    out = boot(HELF, BIG, 55744, ["gfxtest"], shot=shot)
    check("gfxtest ran at 1920x1080", "[mbos] gfxtest drew 1920x1080" in out)

    img = read_ppm(shot) if os.path.exists(shot) else None
    if img is None:
        check("screenshot captured for the stride check", False,
              "no screendump produced")
    else:
        w, h, px = img
        check("screenshot geometry matches the mode",
              (w, h) == (1920, 1080), "got %dx%d" % (w, h))
        # The four corner markers. Right-hand ones are the stride test: with a
        # wrong stride they land somewhere other than the right edge.
        check("top-left marker is red", near(px(0, 0), (255, 0, 0)),
              str(px(0, 0)))
        check("top-right marker is green (stride correct)",
              near(px(w - 1, 0), (0, 255, 0)), str(px(w - 1, 0)))
        check("bottom-left marker is blue", near(px(0, h - 1), (0, 0, 255)),
              str(px(0, h - 1)))
        check("bottom-right marker is yellow (stride correct)",
              near(px(w - 1, h - 1), (255, 255, 0)), str(px(w - 1, h - 1)))
        # Colour bars across the top half, sampled mid-bar.
        bars = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
                (255, 0, 255), (255, 0, 0), (0, 0, 255), (32, 32, 32)]
        bad = [i for i, c in enumerate(bars)
               if not near(px(i * (w // 8) + (w // 16), h // 4), c)]
        check("all eight colour bars land in the right columns", not bad,
              "bars wrong: %r" % bad)
        # The gradient in the bottom half must actually be a gradient.
        top = px(w // 2, h // 2 + 10)[0]
        bot = px(w // 2, h - 10)[0]
        check("gradient increases down the lower half", bot > top + 100,
              "top=%d bottom=%d" % (top, bot))
        os.unlink(shot)

    for p in passed:
        print("  ok    " + p)
    print()
    if failures:
        for f in failures:
            print("FAIL -- " + f)
        sys.exit(1)
    print("PASS -- hi-res framebuffer: caps queried, stride honoured, "
          "modes negotiated.")


if __name__ == "__main__":
    main()
