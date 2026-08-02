"""End-to-end test: link a real bare-metal kernel image with rlink.

The mbos kernel (`examples/rpython2c/mbos`) is a Multiboot1 image that QEMU
boots with a plain `-kernel`. It is the strongest available check on the
linker-script path, because a bare-metal image has requirements a hosted
program does not:

  * the Multiboot header must sit in the first 8 KiB of the loaded image, which
    is why the script puts `*(.multiboot)` at the head of `.text`;
  * the image loads at a fixed physical address (1 MiB) with sections packed
    tightly, not page-aligned per segment;
  * the header itself contains `_load_start` / `_load_end` / `_bss_end`, so the
    script's symbol assignments have to agree with the final layout -- a
    mismatch there is a kernel that loads a truncated image and triple-faults.

The test assembles `boot64.S` with rasm, links the kernel with rlink, and
compares the result against the same link done by GNU `ld`: entry point, key
symbol addresses, segment geometry, and the Multiboot header word for word.

It does not boot the image -- that needs QEMU (`make -C examples/rpython2c/mbos
test`). What it does establish is that rlink's output describes itself exactly
as ld's does.
"""
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rasm_obj
import rlink

REPO = os.path.dirname(os.path.dirname(HERE))
MBOS = os.path.join(REPO, "examples", "rpython2c", "mbos")
SCRIPT = os.path.join(MBOS, "linker64.ld")
BOOT = os.path.join(MBOS, "boot64.S")
# Must stay in the same order as the Makefile's link line -- symbol addresses
# are compared against the ld-built build/mbos.elf, and input order decides
# them.
#
# idt.o here is the gcc-built one from the Makefile. rasm *can* assemble idt.S
# since macro support landed (see rasm_macro_test.py), but its object is 27
# bytes larger: rasm keeps a relocation for branches to a global symbol, so it
# cannot relax nine of them to rel8 the way gas does. Using it would shift
# every symbol address and this test compares those against ld's. boot64.S
# below is still assembled by rasm, so the rasm path stays covered.
COBJS = ["idt.o",
         # COBJS from the mbos Makefile, in order
         "main.o", "console.o", "libmini.o", "dom.o", "html.o", "render.o",
         "net.o", "vbe.o", "irq.o", "kbd.o", "shell.o", "alloc.o", "ramfs.o",
         "mingine_mbos.o",
         # RSOBJS: lowered from .rs by gen_rs.py, linked last
         "rs_editbuf.o", "rs_alloc.o", "rs_tarfs.o"]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def load_segment(path):
    """Return (bytes, vaddr, filesz, memsz, entry) for the single PT_LOAD."""
    with open(path, "rb") as f:
        d = f.read()
    entry = struct.unpack("<Q", d[0x18:0x20])[0]
    phoff = struct.unpack("<Q", d[0x20:0x28])[0]
    off, vaddr = struct.unpack("<QQ", d[phoff + 8:phoff + 24])
    filesz, memsz = struct.unpack("<QQ", d[phoff + 32:phoff + 48])
    return (d[off:off + filesz], vaddr, filesz, memsz, entry)


def symbols(path):
    out = {}
    r = run(["nm", path])
    if r.returncode != 0:
        return out
    for line in r.stdout.decode().splitlines():
        parts = line.split()
        if len(parts) == 3:
            out[parts[2]] = int(parts[0], 16)
    return out


def main():
    if not os.path.exists(SCRIPT) or not os.path.exists(BOOT):
        print("  SKIP  mbos sources not present")
        return 0

    build = os.path.join(MBOS, "build")
    ref = os.path.join(build, "mbos.elf")
    missing = [o for o in COBJS if not os.path.exists(os.path.join(build, o))]
    if missing or not os.path.exists(ref):
        print("  SKIP  run `make -C %s` first (missing %s)"
              % (MBOS, ", ".join(missing) if missing else "mbos.elf"))
        return 0

    work = tempfile.mkdtemp(prefix="rlink_bare_")
    passed = failed = 0

    # ---- assemble the boot file with rasm ----
    boot_obj = os.path.join(work, "boot64.o")
    try:
        with open(BOOT) as f:
            elf = rasm_obj.assemble_to_elf(f.read())
        with open(boot_obj, "wb") as f:
            f.write(bytes(bytearray(elf)))
        print("  ok    rasm assembled boot64.S")
        passed += 1
    except Exception as e:
        print("  FAIL  rasm could not assemble boot64.S: %s" % e)
        return 1

    # ---- link with rlink, driven by the kernel's own linker script ----
    out = os.path.join(work, "mbos_rlink.elf")
    objs = [boot_obj] + [os.path.join(build, o) for o in COBJS]
    r = run([sys.executable, os.path.join(HERE, "rlink.py"),
             "-T", SCRIPT, "-o", out] + objs)
    if r.returncode != 0 or not os.path.exists(out):
        print("  FAIL  rlink: %s" % r.stderr.decode()[-300:])
        return 1
    print("  ok    rlink produced a kernel image")
    passed += 1

    ld_seg = load_segment(ref)
    rl_seg = load_segment(out)

    # ---- entry point ----
    if ld_seg[4] == rl_seg[4]:
        print("  ok    entry point           0x%x" % rl_seg[4])
        passed += 1
    else:
        print("  FAIL  entry point           rlink=0x%x ld=0x%x"
              % (rl_seg[4], ld_seg[4]))
        failed += 1

    # ---- load address and BSS extent ----
    if ld_seg[1] == rl_seg[1]:
        print("  ok    load address          0x%x" % rl_seg[1])
        passed += 1
    else:
        print("  FAIL  load address          rlink=0x%x ld=0x%x"
              % (rl_seg[1], ld_seg[1]))
        failed += 1
    if ld_seg[3] == rl_seg[3]:
        print("  ok    memory size           0x%x (bss extent agrees)"
              % rl_seg[3])
        passed += 1
    else:
        print("  FAIL  memory size           rlink=0x%x ld=0x%x"
              % (rl_seg[3], ld_seg[3]))
        failed += 1

    # ---- the Multiboot header, which the loader reads before anything runs --
    mb_r = struct.unpack("<8I", rl_seg[0][:32])
    mb_l = struct.unpack("<8I", ld_seg[0][:32])
    names = ["magic", "flags", "checksum", "header_addr", "load_addr",
             "load_end_addr", "bss_end_addr", "entry_addr"]
    # load_end legitimately differs: ld deduplicates SHF_MERGE string
    # sections, so its .rodata is a little smaller. What must hold is that the
    # header agrees with *this* image's own layout.
    for k in range(8):
        if names[k] == "load_end_addr":
            continue
        if mb_r[k] == mb_l[k]:
            passed += 1
        else:
            print("  FAIL  multiboot %-14s rlink=0x%08x ld=0x%08x"
                  % (names[k], mb_r[k], mb_l[k]))
            failed += 1
    if failed == 0:
        print("  ok    multiboot header      magic/flags/checksum/addrs agree")

    if (mb_r[0] & 0xFFFFFFFF) != 0x1BADB002:
        print("  FAIL  multiboot magic is wrong")
        failed += 1
    if ((mb_r[0] + mb_r[1] + mb_r[2]) & 0xFFFFFFFF) != 0:
        print("  FAIL  multiboot checksum does not zero out")
        failed += 1
    else:
        print("  ok    multiboot checksum    sums to zero")
        passed += 1

    # ---- the header must describe this image, not ld's ----
    syms = symbols(out)
    if syms.get("_load_start", -1) == mb_r[4] \
            and syms.get("_load_end", -1) == mb_r[5] \
            and syms.get("_bss_end", -1) == mb_r[6]:
        print("  ok    script symbols        _load_start/_load_end/_bss_end "
              "match the header")
        passed += 1
    else:
        print("  FAIL  script symbols disagree with the multiboot header")
        failed += 1

    # ---- the header has to be reachable by the loader ----
    if mb_r[3] - rl_seg[1] < 8192:
        print("  ok    header placement      within the first 8 KiB")
        passed += 1
    else:
        print("  FAIL  multiboot header is beyond the first 8 KiB")
        failed += 1

    # ---- code addresses should land where ld put them ----
    ld_syms = symbols(ref)
    agree = 0
    checked = 0
    for nm in ["_start", "kmain"]:
        if nm in syms and nm in ld_syms:
            checked += 1
            if syms[nm] == ld_syms[nm]:
                agree += 1
            else:
                print("  FAIL  %s at 0x%x, ld puts it at 0x%x"
                      % (nm, syms[nm], ld_syms[nm]))
    if checked > 0 and agree == checked:
        print("  ok    code addresses        %d symbols match ld exactly"
              % checked)
        passed += 1
    elif checked > 0:
        failed += 1

    print("\nrlink bare-metal: %d/%d passed" % (passed, passed + failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
