#!/usr/bin/env python3
"""Check the Tegra 16550-style UART driver (`uart_8250.c`).

No qemu machine models a Tegra, so the Jetson image cannot be booted here.
That leaves a choice between testing nothing and testing what can actually be
tested, and the parts of this driver most likely to be wrong do not need real
hardware to catch:

  * **register spacing.** Tegra puts the 16550's registers 4 bytes apart, not
    1. With the wrong shift, LCR writes land on IIR/FCR and the port is
    configured almost at random -- silently, because every address in the
    range decodes to a real register.
  * **the DLAB dance.** DLL and DLM are aliases of THR and IER, reachable only
    while LCR.DLAB is set. Leaving DLAB set afterwards means every character
    written goes into the divisor latch and nothing is ever transmitted.
  * **flag polarity.** The PL011 waits while its "FIFO full" flag is *set*; a
    16550 waits while "holding register empty" is *clear*. The two loops look
    alike and mean opposite things, so a driver written by analogy with the
    PL011 waits exactly when it should write.

So the driver is compiled with its base pointing at ordinary RAM, run under
`-M virt` with the PL011 as the real console, and the resulting register file
is dumped and checked. The registers are pre-loaded with LSR bits set so the
polling loops complete -- which is also what makes a polarity inversion
visible: it hangs instead, and the image never reaches the end.

What this does **not** prove: that a real Tegra accepts the sequence, that the
clock and divisor are right for the board, or that anything appears on a wire.
Those need hardware. This proves the driver writes what it means to write,
where it means to write it.

    python3 tools/uart_8250_test.py
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# Somewhere inside virt's identity-mapped RAM, well clear of the image (which
# loads at 0x40080000 and is ~16 KiB) and of the stack and page tables.
FAKE_BASE = 0x40200000

# Register indices, and where they land once shifted by 2.
REGS = {
    "THR/DLL": 0,
    "IER/DLM": 1,
    "IIR/FCR": 2,
    "LCR": 3,
    "MCR": 4,
    "LSR": 5,
}

APP = r"""
void uart_init(void);            /* the PL011, our real console */
void uart_puts(char *s);
void uart_puthex(unsigned long v);

void t8250_init(void);           /* the driver under test, renamed */
void t8250_putc(int c);

#define FAKE 0x40200000UL

static volatile unsigned int *reg(int i)
{
    return (volatile unsigned int *)(FAKE + ((unsigned long)i << 2));
}

static void dump(char *tag)
{
    int i;
    uart_puts(tag);
    for (i = 0; i < 8; i = i + 1) {
        uart_puts(" ");
        uart_puthex((unsigned long)*reg(i));
    }
    uart_puts("\n");
}

void kmain(void)
{
    int i;

    uart_init();
    uart_puts("8250 harness\n");

    /* Clear the fake register file, then set the LSR bits the driver polls
     * on: THRE (0x20) and TEMT (0x40). Without these its wait loops never
     * complete -- which is exactly what a polarity inversion would cause, and
     * why a hang here is a meaningful result rather than a broken test. */
    for (i = 0; i < 8; i = i + 1) {
        *reg(i) = 0;
    }
    *reg(5) = 0x60;

    t8250_init();
    dump("AFTERINIT");

    /* THR aliases DLL, so the divisor low byte has to be read before any
     * character is written over it. */
    t8250_putc('A');
    dump("AFTERPUTC");

    uart_puts("DONE\n");
}
"""


def main():
    if subprocess.run(["which", "qemu-system-aarch64"],
                      capture_output=True).returncode != 0:
        print("SKIP: qemu-system-aarch64 not installed")
        return 0

    import baremetal_arm64 as bm

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "harness.c")
        with open(src, "w") as f:
            f.write(APP)
        elf = os.path.join(d, "harness.elf")
        # Build for virt (so there is a real console) but additionally compile
        # uart_8250.c, pointed at RAM and with its entry points renamed so
        # they do not collide with the PL011's.
        defines = [
            "UART8250_BASE=0x%XUL" % FAKE_BASE,
            "UART8250_SHIFT=2",
            "uart_init=t8250_init",
            "uart_putc=t8250_putc",
            "uart_getc=t8250_getc",
            "uart_flush=t8250_flush",
        ]
        objdir = os.path.join(d, "obj")
        os.makedirs(objdir, exist_ok=True)
        objs = []

        # Assemble the boot spine and compile the PL011 side, without linking
        # -- the image cannot link until the 8250 object exists, since the
        # harness calls its renamed entry points.
        try:
            for name in bm.OS_ASM:
                obj = os.path.join(objdir, name.replace(".", "_") + ".o")
                bm.assemble(os.path.join(bm.BAREMETAL, name), obj)
                objs.append(obj)
            # OS_SOURCES includes irq_arm64.c, which needs an interrupt
            # controller behind the intc_* seam. This harness drives no
            # interrupts, so the timer path is dropped and irq_none_arm64.c
            # supplies the irq_dispatch symbol exc_arm64.c calls.
            plain = [n for n in bm.OS_SOURCES if n != "irq_arm64.c"]
            plain = plain + ["irq_none_arm64.c", "uart_arm64.c"]
            for name in plain:
                obj = os.path.join(objdir, name.replace(".", "_") + ".o")
                bm.compile_c(os.path.join(bm.BAREMETAL, name), obj)
                objs.append(obj)
            obj = os.path.join(objdir, "harness.o")
            bm.compile_c(src, obj)
            objs.append(obj)
        except Exception as e:
            print("  FAIL  building the PL011 half: %s" % e)
            return 1

        # And the driver under test, with its own defines.
        try:
            obj = os.path.join(objdir, "uart8250.o")
            bm.compile_c(os.path.join(bm.BAREMETAL, "uart_8250.c"),
                         obj, defines)
            objs.append(obj)
        except Exception as e:
            print("  FAIL  compiling uart_8250.c: %s" % e)
            return 1

        rc = subprocess.run(
            [sys.executable, os.path.join(HERE, "rpy_lib", "rlink.py"),
             "-T", os.path.join(bm.BAREMETAL, "virt_arm64.ld"),
             "-o", elf] + objs, capture_output=True, text=True)
        if rc.returncode != 0:
            print("  FAIL  linking: %s" % (rc.stdout + rc.stderr).strip())
            return 1
        out = bm.qemu_run(elf, timeout=60)

    if "DONE" not in out:
        if "8250 harness" in out:
            print("  FAIL  the driver hung: a wait loop never completed. "
                  "Either the LSR.THRE test is inverted (the PL011 waits "
                  "while its flag is set; a 16550 waits while THRE is "
                  "clear), or the register shift is wrong and the poll is "
                  "reading an address that is not LSR.")
        else:
            print("  FAIL  image did not run")
        for line in out.replace("\r", "").split("\n")[-8:]:
            if line.strip():
                print("        | " + line)
        return 1

    def row(tag):
        m = re.search(r"%s((?:\s+0x[0-9a-f]+)+)" % tag, out)
        if not m:
            return None
        return [int(x, 16) for x in m.group(1).split()]

    after_init = row("AFTERINIT")
    after_putc = row("AFTERPUTC")
    if not after_init or not after_putc:
        print("  FAIL  could not read the register dump back")
        return 1

    fails = []
    # 408 MHz / (16 * 115200) = 221 = 0xDD.
    want_div = 408000000 // (16 * 115200)
    if after_init[0] != (want_div & 0xFF):
        fails.append("DLL is 0x%02x, expected 0x%02x -- the divisor low byte "
                     "did not reach offset 0"
                     % (after_init[0], want_div & 0xFF))
    if after_init[1] != ((want_div >> 8) & 0xFF):
        fails.append("DLM is 0x%02x, expected 0x%02x"
                     % (after_init[1], (want_div >> 8) & 0xFF))
    # LCR must end at 8N1 with DLAB *clear*; 0x83 would mean DLAB was left set.
    if after_init[3] != 0x03:
        extra = " (DLAB still set: every character would go to the divisor "\
                "latch)" if after_init[3] & 0x80 else ""
        fails.append("LCR is 0x%02x, expected 0x03%s" % (after_init[3], extra))
    if after_init[2] != 0x07:
        fails.append("FCR is 0x%02x, expected 0x07 (enable + clear both FIFOs)"
                     % after_init[2])
    if after_init[4] != 0x03:
        fails.append("MCR is 0x%02x, expected 0x03 (DTR + RTS)"
                     % after_init[4])
    # A write to any register other than these means the shift is wrong.
    if after_init[6] or after_init[7]:
        fails.append("registers 6/7 were written (0x%02x 0x%02x) -- the "
                     "register shift is wrong and writes are landing past "
                     "their intended offsets"
                     % (after_init[6], after_init[7]))
    if after_putc[0] != ord('A'):
        fails.append("THR is 0x%02x after writing 'A', expected 0x41"
                     % after_putc[0])

    if fails:
        for f in fails:
            print("  FAIL  " + f)
        print("\ntegra 8250 uart: FAILED")
        return 1

    print("  PASS  divisor 0x%02x written to DLL/DLM behind DLAB"
          % (want_div & 0xFF))
    print("  PASS  LCR left at 8N1 with DLAB clear")
    print("  PASS  FIFOs enabled and cleared, DTR/RTS asserted")
    print("  PASS  register writes land on the 4-byte Tegra spacing")
    print("  PASS  a character reaches THR, and the THRE poll completes")
    print("\ntegra 8250 uart: OK (register-level; not verified on hardware)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
