#!/usr/bin/env python3
"""baremetal_arm64.py - build and run a bare-metal AArch64 image.

This is the AArch64 counterpart to the x86-64 path in shivycx_baremetal.py.
It deliberately does *not* reuse minikraft: that mini-OS is 32-bit x86, and
its drivers are a VGA text console, a PS/2 keyboard and a PIC/PIT timer, none
of which exist on an AArch64 machine. What carries over is the *shape* of the
pipeline, not the OS.

Everything below the linker is ours:

    C  ->  ShivyCX  ->  rasm  ->  .o
    S  ->  rasm     ->  .o          }-> ld -T virt_arm64.ld -> bootable ELF

The application provides ``void kmain(void)``; boot_arm64.S calls it once the
secondary cores are parked, EL1 is entered, FP is enabled, the vectors are
installed, the stack is set and .bss is cleared.

    python3 tools/baremetal_arm64.py app.c -o kernel.elf --run
    python3 tools/baremetal_arm64.py app.c --run --machine virt

The link is done by our own ``rlink``, which reads the linker script, so no
external tool is involved at any stage. ``--gnu-ld`` switches the final link to
``aarch64-linux-gnu-ld`` instead, which is useful as an oracle: the two should
produce images that behave identically, and rlink_script_test.py checks exactly
that.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BAREMETAL = os.path.join(ROOT, "baremetal64")

sys.path.insert(0, os.path.join(HERE, "rpy_lib"))

# The OS pieces, in link order. Unlike the x86 path there is no symbol-closure
# step yet: the set is small enough that pulling in all of it costs a few KiB,
# and a closure over three files mostly tests the closure.
# console_arm64.c holds the formatting and is board-independent. The actual
# driver -- which defines uart_init and uart_putc -- comes from the board
# profile's "console" entry, since exactly one of them may be linked.
OS_SOURCES = ["console_arm64.c", "exc_arm64.c", "mmu_arm64.c",
              "irq_arm64.c"]

# Board profiles. Only three things actually differ between an emulated virt
# machine and a Raspberry Pi: where the image is loaded, where the PL011 sits,
# and whether there is a GPIO block that must route the UART to a pin. The
# boot, exception and MMU code is shared unchanged.
#
# `irq` marks whether the board has a GICv2. The Pi 3's BCM2837 does not -- it
# uses a bespoke BCM interrupt controller plus per-core "ARM local" registers
# -- so the GIC and timer files are left out of the Pi build rather than
# compiled and silently writing to addresses that decode to something else.
BOARDS = {
    "virt": {
        "script": "virt_arm64.ld",
        "machine": "virt",
        "cpu": "cortex-a57",
        # RAM at 0x40000000, peripherals in the gigabyte below it.
        "defines": ["RAM_BASE=0x40000000UL", "PERIPH_BASE=0x0UL",
                    "PERIPH_SIZE=0x40000000UL"],
        "irq": True,
        "intc": "gic_arm64.c",
        "console": "uart_arm64.c",
        "desc": "qemu-system-aarch64 -M virt (PL011 at 0x09000000)",
    },
    "raspi3": {
        "script": "raspi_arm64.ld",
        "machine": "raspi3b",
        "cpu": "cortex-a53",
        # Peripherals at 0x3F000000 on the BCM2837.
        # RAM starts at 0 and the peripherals sit at 0x3F000000, so both
        # land in gigabyte 0 and the MMU picks attributes per 2 MiB block.
        "defines": ["PL011_BASE=0x3F201000", "RASPI_GPIO_BASE=0x3F200000",
                    # The window has to reach past 0x40000000: the BCM
                    # peripherals are at 0x3F000000 but the ARM *local*
                    # peripherals that route the generic timer are at
                    # 0x40000000, in the next gigabyte.
                    "RAM_BASE=0x0UL", "PERIPH_BASE=0x3F000000UL",
                    "PERIPH_SIZE=0x1100000UL"],
        # The BCM2837 has no GIC: the generic timer is routed by the ARM
        # local peripherals at 0x40000000, and GPU sources by a legacy
        # controller at 0x3F00B200.
        "irq": True,
        "intc": "bcm_irq_arm64.c",
        "console": "uart_arm64.c",
        "desc": "Raspberry Pi 3 / Zero 2 W (BCM2837, load at 0x80000)",
    },
    "jetson": {
        "script": "jetson_arm64.ld",
        # No qemu machine models a Tegra, so this cannot be booted here.
        "machine": None,
        "cpu": "cortex-a57",      # Jetson Nano / TX1 are A57
        # UART-A at 0x70006000. Tegra spaces the 16550 registers 4 bytes
        # apart, hence shift 2 -- the unshifted layout would put LCR writes
        # on IIR/FCR and configure the port at random, with no error.
        # Tegra X1 has a GICv2. Unlike the Pi 3's bespoke controller, that
        # means gic_arm64.c and the generic timer work unchanged -- only the
        # two base addresses move. Note the distributor-to-CPU-interface gap
        # is 0x1000 here, not virt's 0x10000.
        "defines": ["UART8250_BASE=0x70006000", "UART8250_SHIFT=2",
                    "GICD_BASE=0x50041000", "GICC_BASE=0x50042000",
                    # Tegra X1 UART-A is INTID 68, not virt's 33. Untested:
                    # no qemu machine models a Tegra.
                    "UART_IRQ=68",
                    # DRAM starts at 0x80000000 (gigabyte 2); the GIC and the
                    # UART both sit in gigabyte 1.
                    "RAM_BASE=0x80000000UL", "PERIPH_BASE=0x50000000UL",
                    "PERIPH_SIZE=0x30000000UL"],
        "irq": True,
        "intc": "gic_arm64.c",
        "console": "uart_8250.c",
        "desc": "NVIDIA Jetson Nano / TX1 (Tegra X1, U-Boot booti at "
                "0x80080000)",
    },
    "raspi4": {
        "script": "raspi_arm64.ld",
        # No qemu machine models a BCM2711. Booting a raspi4 image under
        # -M raspi3b silently produces nothing, because the peripherals it
        # writes to (0xFE000000) simply are not there -- so --run refuses
        # rather than leaving a blank console to be misread as a hang.
        "machine": None,
        "cpu": "cortex-a72",
        # Peripherals move to 0xFE000000 on the BCM2711.
        # The BCM2711 does have a GIC-400 (GICD 0xFF841000, GICC 0xFF842000),
        # but nothing here has been able to exercise it, so it is left off
        # rather than shipped untested.
        "defines": ["PL011_BASE=0xFE201000", "RASPI_GPIO_BASE=0xFE200000",
                    "RAM_BASE=0x0UL", "PERIPH_BASE=0xFE000000UL",
                    "PERIPH_SIZE=0x2000000UL"],
        "irq": False,
        "console": "uart_arm64.c",
        "desc": "Raspberry Pi 4 / 400 (BCM2711, load at 0x80000)",
    },
}
OS_ASM = ["boot_arm64.S", "vectors_arm64.S", "mmu_enable_arm64.S",
          "timer_arm64.S"]

RLINK = os.path.join(HERE, "rpy_lib", "rlink.py")
GNU_LD = "aarch64-linux-gnu-ld"


def log(*a):
    print("[baremetal-arm64]", *a)


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return p.returncode, p.stdout, p.stderr


def compile_c(src, obj, defines=None):
    """Compile one C file to an AArch64 object with ShivyCX + rasm.

    SHIVYC_RASM is what makes this work at all off an AArch64 host: without
    it ShivyCX writes AArch64 assembly and hands it to the *host* assembler,
    which rejects it.
    """
    env = dict(os.environ)
    env["SHIVYC_RASM"] = "1"
    # ShivyCX writes its intermediate .s beside the *input*, so compiling
    # baremetal64/uart_arm64.c would drop uart_arm64.s into the source tree.
    # Compile a copy in the object directory instead.
    work = os.path.join(os.path.dirname(obj), os.path.basename(src))
    if os.path.abspath(work) != os.path.abspath(src):
        shutil.copyfile(src, work)
    cmd = [sys.executable, "-m", "shivyc.main", work, "-c", "-o", obj,
           "--target", "arm64"]
    for d in (defines or []):
        cmd.append("-D" + d)
    rc, out, err = run(cmd, env=env, cwd=ROOT)
    if rc != 0 or not os.path.exists(obj):
        raise RuntimeError("ShivyCX failed on %s:\n%s%s"
                           % (os.path.basename(src), out, err))
    return obj


def assemble(src, obj):
    """Assemble one .S file with our own rasm."""
    import rasm_obj
    with open(src) as f:
        text = f.read()
    data = rasm_obj.assemble_to_elf(text, "arm64")
    with open(obj, "wb") as f:
        f.write(bytes(data))
    return obj


def build(app_sources, out_elf, objdir, script=None, extra_os=None,
          gnu_ld=False, board="virt", extra_defines=None,
          vectors=None, extra_asm=None):
    prof = BOARDS[board]
    os.makedirs(objdir, exist_ok=True)
    script = script or os.path.join(BAREMETAL, prof["script"])
    defines = prof["defines"] + list(extra_defines or [])
    objs = []

    # Boot stub first: it must be the first object so .text.boot -- and so
    # _start -- lands at the image's entry address.
    asm_sources = list(OS_ASM)
    if vectors:
        asm_sources = [vectors if n == "vectors_arm64.S" else n
                       for n in asm_sources]
    asm_sources = asm_sources + list(extra_asm or [])
    if not prof["irq"]:
        asm_sources = [n for n in asm_sources if n != "timer_arm64.S"]
    for name in asm_sources:
        obj = os.path.join(objdir, os.path.basename(name).replace(".", "_")
                           + ".o")
        # A bare name is a file in baremetal64/; anything that exists as
        # given (an absolute path, or one relative to the invocation) is used
        # as-is, so generated assembly can be pulled in from a build dir.
        if os.path.isabs(name) or os.path.exists(name):
            src_path = name
        else:
            src_path = os.path.join(BAREMETAL, name)
        assemble(src_path, obj)
        objs.append(obj)
        log("rasm    ", os.path.basename(name))

    sources = extra_os or (OS_SOURCES + [prof["console"]])
    if prof["irq"] and prof.get("intc"):
        sources = sources + [prof["intc"]]
    if not prof["irq"]:
        # No GICv2 on this board; leave the interrupt files out entirely.
        sources = [n for n in sources if n != "irq_arm64.c"]
        # exc_arm64.c calls irq_dispatch() unconditionally, so a board with no
        # interrupt controller still needs the symbol; irq_none_arm64.c
        # supplies a counting stub rather than a dangling reference.
        sources = sources + ["irq_none_arm64.c"]
    for name in sources:
        path = os.path.join(BAREMETAL, name)
        if not os.path.exists(path):
            continue
        obj = os.path.join(objdir, name.replace(".", "_") + ".o")
        compile_c(path, obj, defines)
        objs.append(obj)
        log("shivycx ", name)

    for src in app_sources:
        obj = os.path.join(objdir,
                           os.path.basename(src).replace(".", "_") + ".o")
        compile_c(src, obj, defines)
        objs.append(obj)
        log("shivycx ", os.path.basename(src), "(app)")

    if gnu_ld:
        cmd = [GNU_LD, "-T", script, "-o", out_elf] + objs
        which = "aarch64-linux-gnu-ld"
    else:
        cmd = [sys.executable, RLINK, "-T", script, "-o", out_elf] + objs
        which = "rlink"
    rc, out, err = run(cmd)
    # GNU ld warns about an RWX load segment; for a bare-metal image that is
    # exactly what we want, so it is not worth surfacing.
    err = "\n".join([l for l in err.split("\n")
                     if l.strip() and "RWX permissions" not in l])
    if rc != 0 or not os.path.exists(out_elf):
        raise RuntimeError("link failed:\n%s%s" % (out, err))
    log("%-8s" % which, out_elf, "(%d bytes)" % os.path.getsize(out_elf))
    return out_elf


def qemu_run(elf, machine="virt", cpu="cortex-a57", timeout=30):
    """Boot the image under qemu-system-aarch64 and return its console output.

    -nodefaults matters: without it qemu adds a virtio net device and fails
    looking for a ROM file it does not have, before our image ever runs.
    """
    cmd = ["qemu-system-aarch64", "-M", machine, "-cpu", cpu,
           "-nographic", "-nodefaults", "-serial", "mon:stdio",
           "-kernel", elf]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        # The image parks in a wfi loop rather than exiting, so a timeout is
        # the normal end of a successful run, not a failure.
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return out


def main(argv):
    args = argv[1:]
    sources = []
    out = None
    do_run = False
    gnu_ld = False
    board = "virt"
    vectors = None
    extra_asm = []
    defines = []
    machine = "virt"
    cpu = "cortex-a57"
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-o":
            out = args[i + 1]
            i += 1
        elif a == "--run":
            do_run = True
        elif a == "--vectors":
            # Swap the vector table, e.g. for the register-partitioned
            # preemptive scheduler whose IRQ slot branches to a generated
            # switcher instead of the generic exc_common path.
            vectors = args[i + 1]
            i += 1
        elif a == "-D":
            defines.append(args[i + 1])
            i += 1
        elif a[0:2] == "-D" and len(a) > 2:
            defines.append(a[2:])
        elif a == "--extra-asm":
            extra_asm.append(args[i + 1])
            i += 1
        elif a == "--gnu-ld":
            gnu_ld = True
        elif a == "--board":
            board = args[i + 1]
            if board not in BOARDS:
                print("unknown board %r; known: %s"
                      % (board, ", ".join(sorted(BOARDS))))
                return 2
            i += 1
        elif a == "--boards":
            for k in sorted(BOARDS):
                print("  %-8s %s" % (k, BOARDS[k]["desc"]))
            return 0
        elif a == "--machine":
            machine = args[i + 1]
            i += 1
        elif a == "--cpu":
            cpu = args[i + 1]
            i += 1
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            sources.append(a)
        i += 1

    if not sources:
        print(__doc__)
        return 2

    out = out or os.path.join(ROOT, "build", "arm64bm", "kernel.elf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    objdir = os.path.join(os.path.dirname(out), "obj")
    build(sources, out, objdir, gnu_ld=gnu_ld, board=board,
          vectors=vectors, extra_asm=extra_asm, extra_defines=defines)

    if do_run:
        prof = BOARDS[board]
        if prof["machine"] is None and machine == "virt":
            log("cannot run %s here: no qemu machine models this board's "
                "peripherals, so the image would boot to a silent console. "
                "The image itself is built and can be copied to hardware."
                % board)
            return 0
        if machine == "virt":
            machine = prof["machine"]
        if cpu == "cortex-a57":
            cpu = prof["cpu"]
        log("booting under qemu-system-aarch64 -M %s -cpu %s" % (machine, cpu))
        print(qemu_run(out, machine, cpu))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
