#!/usr/bin/env python3
"""Check that `gic_arm64.c` honours relocated distributor and CPU-interface
bases -- the Jetson configuration.

qemu's virt machine puts its GIC at a fixed address and offers no way to move
it, and no qemu machine models a Tegra at all. So a moved GIC cannot be
exercised by booting one. What can be checked is that the driver *writes where
it is told to*, which is the whole of what parameterising the bases means.

The two bases are separate parameters rather than a base plus a constant,
because the gap between them is not architectural:

    qemu virt      GICD 0x08000000   GICC 0x08010000    gap 0x10000
    Jetson (T210)  GICD 0x50041000   GICC 0x50042000    gap  0x1000

A driver that derived the CPU interface as `GICD_BASE + 0x10000` would compile
and run on virt and put every CPU-interface write 0xF000 into the
distributor's address space on a Jetson -- where the writes would be accepted
and quietly do something else. So this test deliberately uses two RAM regions
**0x1000 apart**, the Tegra spacing: if the offset were assumed rather than
passed, the CPU-interface region would stay empty and the distributor region
would collect writes it should never see.

What this does not prove: that a real Tegra GIC accepts the sequence, or that
an interrupt is ever delivered on one. Those need hardware. It proves the
driver's writes land at the offsets the two parameters imply.

    python3 tools/gic_base_test.py
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# Two scratch regions inside virt's identity-mapped RAM, a Tegra-like 0x1000
# apart, well clear of the image, stack and page tables.
FAKE_GICD = 0x40300000
FAKE_GICC = 0x40301000

TIMER_PPI = 30

APP = r"""
void uart_init(void);
void uart_puts(char *s);
void uart_puthex(unsigned long v);

void intc_init(void);
void intc_init_cpu(void);
void intc_enable_irq(int irq);

#define FD 0x40300000UL
#define FC 0x40301000UL

static volatile unsigned int *d(unsigned long off)
{
    return (volatile unsigned int *)(FD + off);
}

static volatile unsigned int *c(unsigned long off)
{
    return (volatile unsigned int *)(FC + off);
}

static void show(char *tag, unsigned long v)
{
    uart_puts(tag);
    uart_puthex(v);
    uart_puts("\n");
}

void kmain(void)
{
    unsigned long i;
    unsigned long dirty;

    uart_init();
    uart_puts("gic harness\n");

    /* Clear both regions. */
    for (i = 0; i < 0x1000; i = i + 4) {
        *d(i) = 0;
        *c(i) = 0;
    }
    /* TYPER low bits = 1 gives (1+1)*32 = 64 interrupts, enough to exercise
     * the SPI path without a huge loop. */
    *d(0x004) = 1;

    intc_init();
    intc_init_cpu();
    intc_enable_irq(30);

    show("DCTLR=", (unsigned long)*d(0x000));
    show("CCTLR=", (unsigned long)*c(0x000));
    show("CPMR=", (unsigned long)*c(0x004));
    show("ISENABLE0=", (unsigned long)*d(0x100));
    show("IPRIO0=", (unsigned long)*d(0x400));
    show("ITARGET32=", (unsigned long)*d(0x820));

    /* How much of the CPU-interface region was written at all. If the driver
     * derived GICC from GICD by a fixed offset, this stays zero. */
    dirty = 0;
    for (i = 0; i < 0x1000; i = i + 4) {
        if (*c(i) != 0) {
            dirty = dirty + 1;
        }
    }
    show("CDIRTY=", dirty);

    uart_puts("DONE\n");
}
"""


def main():
    if subprocess.run(["which", "qemu-system-aarch64"],
                      capture_output=True).returncode != 0:
        print("SKIP: qemu-system-aarch64 not installed")
        return 0

    import baremetal_arm64 as bm

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "harness.c")
        with open(src, "w") as f:
            f.write(APP)
        elf = os.path.join(tmp, "harness.elf")
        objdir = os.path.join(tmp, "obj")
        os.makedirs(objdir, exist_ok=True)
        objs = []

        # The real GIC and timer are left out: this image drives a GIC in RAM,
        # and linking the interrupt path as well would mean two callers of the
        # same driver. irq_none_arm64.c supplies the irq_dispatch symbol
        # exc_arm64.c needs.
        plain = ["console_arm64.c", "exc_arm64.c", "mmu_arm64.c",
                 "irq_none_arm64.c", "uart_arm64.c"]
        try:
            for name in ["boot_arm64.S", "vectors_arm64.S",
                         "mmu_enable_arm64.S"]:
                obj = os.path.join(objdir, name.replace(".", "_") + ".o")
                bm.assemble(os.path.join(bm.BAREMETAL, name), obj)
                objs.append(obj)
            for name in plain:
                obj = os.path.join(objdir, name.replace(".", "_") + ".o")
                bm.compile_c(os.path.join(bm.BAREMETAL, name), obj)
                objs.append(obj)
            obj = os.path.join(objdir, "harness.o")
            bm.compile_c(src, obj)
            objs.append(obj)
            # The driver under test, pointed at the two scratch regions.
            obj = os.path.join(objdir, "gic.o")
            bm.compile_c(os.path.join(bm.BAREMETAL, "gic_arm64.c"), obj,
                         ["GICD_BASE=0x%XUL" % FAKE_GICD,
                          "GICC_BASE=0x%XUL" % FAKE_GICC])
            objs.append(obj)
        except Exception as e:
            print("  FAIL  build: %s" % e)
            return 1

        rc = subprocess.run(
            [sys.executable, os.path.join(HERE, "rpy_lib", "rlink.py"),
             "-T", os.path.join(bm.BAREMETAL, "virt_arm64.ld"),
             "-o", elf] + objs, capture_output=True, text=True)
        if rc.returncode != 0:
            print("  FAIL  link: %s" % (rc.stdout + rc.stderr).strip())
            return 1
        out = bm.qemu_run(elf, timeout=60)

    if "DONE" not in out:
        print("  FAIL  image did not run to completion")
        for line in out.replace("\r", "").split("\n")[-8:]:
            if line.strip():
                print("        | " + line)
        return 1

    def val(key):
        m = re.search(r"\b%s0x([0-9a-f]+)" % key, out)
        return int(m.group(1), 16) if m else None

    dctlr = val("DCTLR=")
    cctlr = val("CCTLR=")
    cpmr = val("CPMR=")
    isen = val("ISENABLE0=")
    iprio = val("IPRIO0=")
    itarget = val("ITARGET32=")
    cdirty = val("CDIRTY=")

    fails = []
    if cdirty == 0:
        fails.append(
            "nothing was written to the CPU-interface region at all. The "
            "driver is not using GICC_BASE -- most likely it derives the CPU "
            "interface from the distributor by a fixed offset, which is "
            "correct on virt and wrong on a Jetson.")
    if dctlr != 1:
        fails.append("GICD_CTLR is 0x%x, expected 1 (distributor enabled)"
                     % dctlr)
    if cctlr != 1:
        fails.append("GICC_CTLR is 0x%x, expected 1 (CPU interface enabled)"
                     % cctlr)
    if cpmr != 0xF0:
        fails.append("GICC_PMR is 0x%x, expected 0xF0. At 0 every interrupt "
                     "is masked and nothing is ever delivered." % cpmr)
    if isen is None or not (isen & (1 << TIMER_PPI)):
        fails.append("ISENABLER bit %d is clear after intc_enable_irq(%d): "
                     "0x%x" % (TIMER_PPI, TIMER_PPI, isen or 0))
    if iprio != 0xA0A0A0A0:
        fails.append("IPRIORITYR is 0x%x, expected 0xa0a0a0a0" % iprio)
    if itarget != 0x01010101:
        fails.append("ITARGETSR for SPIs is 0x%x, expected 0x01010101 "
                     "(routed to core 0)" % itarget)

    if fails:
        for f in fails:
            print("  FAIL  " + f)
        print("\ngic base parameterisation: FAILED")
        return 1

    print("  PASS  distributor and CPU interface both enabled at their own "
          "bases")
    print("  PASS  CPU-interface writes land 0x1000 away, not 0x10000 "
          "(%d words written)" % cdirty)
    print("  PASS  PMR 0xf0, priorities 0xa0, SPIs routed to core 0")
    print("  PASS  intc_enable_irq(%d) sets the right ISENABLER bit"
          % TIMER_PPI)
    print("\ngic base parameterisation: OK (register-level; Tegra not "
          "verified on hardware)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
