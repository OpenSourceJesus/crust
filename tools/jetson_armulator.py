"""jetson_armulator.py - boot a bare-metal image under armulator.

Named for the Jetson, which is what it was written for, but it boots the
Raspberry Pi 4 too -- the other board with no qemu machine. Pass ``--board``.

No qemu machine models a Tegra, so ``baremetal_arm64.py --board jetson --run``
refuses: it will build the image but has nowhere to run it, and booting a
Jetson image under ``-M virt`` would write to peripherals that are not there
and produce a silent console rather than an error.

armulator (https://github.com/crustos/armulator) fills that gap. It is a pure
Python ARM emulator with a Cortex-A57 model -- which is the Nano's actual core
-- and a Tegra X1 board: GIC-400 at 0x50040000, UART-A at 0x70006000, the
Tegra GPIO block, and the architected generic timer. That is exactly the set a
bare-metal image here touches, which is why this works despite armulator
modelling only a handful of the T210's several dozen peripherals. A vendor
Linux kernel would get nowhere.

    python3 tools/jetson_armulator.py                    # build and boot
    python3 tools/jetson_armulator.py --board raspi4     # BCM2711 GIC-400
    python3 tools/jetson_armulator.py examples/baremetal/kernel_arm64.c
    python3 tools/jetson_armulator.py --elf build/jetson.elf
    python3 tools/jetson_armulator.py --armulator ../armulator

What this is and is not: armulator emulates the CPU, the interrupt controller
and the console. It is not the SoC. Anything depending on Tegra's clock and
reset controller, its memory controller, power management, display or USB is
absent, and a passing run here is evidence about the image's CPU, MMU,
exception and interrupt behaviour -- not evidence that a Jetson would boot it.
That remains unverified on hardware.
"""

import argparse
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_APP = os.path.join(ROOT, "examples", "baremetal", "kernel_arm64.c")

#: Where to look for armulator when it is not installed.
CANDIDATE_PATHS = [
    os.environ.get("ARMULATOR_PATH", ""),
    os.path.join(os.path.dirname(ROOT), "armulator"),
    os.path.expanduser("~/armulator"),
]


def find_armulator(explicit=None):
    """Put armulator on sys.path, or explain how to get it."""
    for path in ([explicit] if explicit else []) + CANDIDATE_PATHS:
        if path and os.path.isdir(os.path.join(path, "armulator")):
            sys.path.insert(0, path)
            return path
    try:
        import armulator  # noqa: F401
        return "(installed)"
    except ImportError:
        raise SystemExit(
            "armulator not found. Either:\n"
            "  git clone https://github.com/crustos/armulator.git "
            "../armulator\n"
            "or pass --armulator PATH, or set ARMULATOR_PATH."
        )


#: Boards this can boot, and the armulator class that models each.
#: Both are boards qemu has no machine for, which is the whole reason this
#: exists. The Pi 3 and virt are deliberately absent: qemu boots those, and
#: qemu is the better oracle where it is available.
ARMULATOR_BOARDS = {
    "jetson": ("JetsonNanoA64", "Cortex-A57, Tegra X1 map"),
    "raspi4": ("RaspberryPi4A64", "Cortex-A72, BCM2711 map"),
}


def build_image(app, out, board="jetson"):
    """Build ``app`` for ``board`` with baremetal_arm64.py."""
    cmd = [sys.executable, os.path.join(HERE, "baremetal_arm64.py"),
           app, "--board", board, "-o", out]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("build failed:\n%s%s" % (proc.stdout, proc.stderr))
    return out


def load_segments(path):
    """Return (entry, [(vaddr, bytes)]) for a little-endian AArch64 ELF64.

    Only PT_LOAD is honoured, and each segment is zero-extended from filesz to
    memsz so .bss is present -- boot_arm64.S clears it, but the memory has to
    exist first.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:4] != b"\x7fELF" or data[4] != 2:
        raise SystemExit("%s is not an ELF64 image" % path)
    entry, phoff = struct.unpack_from("<QQ", data, 0x18)
    phentsize, phnum = struct.unpack_from("<HH", data, 0x36)

    segments = []
    for i in range(phnum):
        off = phoff + i * phentsize
        p_type, = struct.unpack_from("<I", data, off)
        if p_type != 1:                          # PT_LOAD
            continue
        p_offset, p_vaddr, _paddr, p_filesz, p_memsz = struct.unpack_from(
            "<QQQQQ", data, off + 0x08)
        blob = data[p_offset:p_offset + p_filesz]
        blob += b"\x00" * (p_memsz - p_filesz)
        segments.append((p_vaddr, blob))
    if not segments:
        raise SystemExit("%s has no PT_LOAD segments" % path)
    return entry, segments


def boot(elf, max_instructions=2000000, ram_size=0x400000, quiet=False,
         expect=None, slice_size=50000, board_name="jetson"):
    """Boot ``elf`` on the named armulator board. Returns the console text.

    Runs in slices so the boot can stop as soon as ``expect`` appears. Once
    timer interrupts are live the firmware's parked halt loop is entered and
    left on every tick, so ``Board.run``'s tight-self-branch detection never
    fires and the run would otherwise always burn the full instruction
    budget.
    """
    import armulator.boards as boards_module

    cls_name = ARMULATOR_BOARDS[board_name][0]
    entry, segments = load_segments(elf)
    board = getattr(boards_module, cls_name)(ram_size=ram_size)

    for vaddr, blob in segments:
        end = vaddr + len(blob)
        if end > board.RAM_BASE + ram_size:
            raise SystemExit(
                "segment at 0x%X..0x%X does not fit in %d KiB of RAM at "
                "0x%X -- pass a larger --ram"
                % (vaddr, end, ram_size // 1024, board.RAM_BASE))
        board.load(vaddr, blob)

    console = bytearray()

    def emit(byte):
        console.append(byte)
        if not quiet:
            sys.stdout.write(chr(byte))
            sys.stdout.flush()

    board.uart.tx_callbacks.append(emit)

    board.start(entry)
    executed = 0
    while executed < max_instructions:
        chunk = min(slice_size, max_instructions - executed)
        executed += board.run(chunk)
        if board.halted or board.fault_loop:
            break
        if expect and expect.encode() in bytes(console):
            break

    return console.decode("utf-8", errors="replace"), board, executed


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Boot a bare-metal image under armulator.")
    parser.add_argument("app", nargs="?", default=DEFAULT_APP,
                        help="C source to build (default: kernel_arm64.c)")
    parser.add_argument("--board", default="jetson",
                        choices=sorted(ARMULATOR_BOARDS),
                        help="which board to build for and boot "
                             "(default: jetson)")
    parser.add_argument("--elf", help="boot this prebuilt image instead")
    parser.add_argument("--armulator", help="path to an armulator checkout")
    parser.add_argument("--ram", type=lambda s: int(s, 0), default=0x400000,
                        help="RAM size, default 4 MiB")
    parser.add_argument("--max-instructions", type=lambda s: int(s, 0),
                        default=2000000)
    parser.add_argument("--expect", default="all stages ok",
                        help="text the console must contain to pass")
    parser.add_argument("--quiet", action="store_true",
                        help="do not stream the console")
    args = parser.parse_args(argv)

    where = find_armulator(args.armulator)

    tmp = None
    try:
        if args.elf:
            elf = args.elf
        else:
            tmp = tempfile.mkdtemp(prefix="armulator-")
            elf = build_image(args.app,
                              os.path.join(tmp, args.board + ".elf"),
                              board=args.board)

        cls_name, description = ARMULATOR_BOARDS[args.board]
        print("[jetson-armulator] armulator: %s" % where)
        print("[jetson-armulator] image:     %s" % elf)
        print("[jetson-armulator] booting %s (%s)\n"
              % (cls_name, description))

        text, board, executed = boot(
            elf, max_instructions=args.max_instructions,
            ram_size=args.ram, quiet=args.quiet, expect=args.expect,
            board_name=args.board)
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    if args.quiet:
        print(text)
    print("\n[jetson-armulator] %d instructions, halted=%s fault_loop=%s"
          % (executed, board.halted, board.fault_loop))

    if board.fault_loop:
        print("[jetson-armulator] FAIL: faulting through the vector table")
        return 1
    if args.expect and args.expect not in text:
        print("[jetson-armulator] FAIL: console never contained %r"
              % args.expect)
        return 1
    print("[jetson-armulator] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
