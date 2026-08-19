"""Differential test: rasm_arm64's system instructions vs `aarch64-linux-gnu-as`.

These are the instructions bare metal needs and user-mode code never touches:
system-register moves, barriers, the exception return, TLB/cache maintenance,
and `adr`. They are also the instructions where a wrong encoding is *least*
likely to announce itself -- a misplaced field in an `msr` produces a legal
instruction that writes a different register, and the machine simply misbehaves
much later, in the MMU or the vector table, with nothing pointing back here.

So every case is assembled twice and compared byte for byte.

    python3 rasm_arm64_sys_test.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rasm_arm64        # noqa: E402

AS = "aarch64-linux-gnu-as"
OBJCOPY = "aarch64-linux-gnu-objcopy"

CASES = [
    # -- reading system registers --------------------------------------
    "mrs x0, currentel",
    "mrs x1, midr_el1",
    "mrs x2, mpidr_el1",
    "mrs x0, sctlr_el1",
    "mrs x3, esr_el1",
    "mrs x4, far_el1",
    "mrs x5, elr_el1",
    "mrs x6, spsr_el1",
    "mrs x7, vbar_el1",
    "mrs x8, id_aa64mmfr0_el1",
    "mrs x9, cntfrq_el0",
    "mrs x0, cntpct_el0",
    "mrs x0, cntvct_el0",
    "mrs x0, cntp_ctl_el0",
    "mrs x0, cntp_tval_el0",
    "mrs x0, cntp_cval_el0",
    "mrs x0, cntv_ctl_el0",
    "mrs x0, cntv_tval_el0",
    "mrs x0, cnthctl_el2",
    "mrs x0, cntvoff_el2",
    "mrs x0, isr_el1",
    "msr cntp_ctl_el0, x0",
    "msr cntp_tval_el0, x1",
    "msr cntp_cval_el0, x2",
    "msr cntv_ctl_el0, x3",
    "msr cnthctl_el2, x4",
    "msr cntvoff_el2, x5",
    "mrs x0, cntpct_el0",
    "mrs x0, cntvct_el0",
    "mrs x0, cntp_ctl_el0",
    "mrs x0, cntp_tval_el0",
    "mrs x0, cntp_cval_el0",
    "mrs x0, cntv_ctl_el0",
    "mrs x0, isr_el1",
    "mrs x0, cntkctl_el1",
    "mrs x0, cnthctl_el2",
    "mrs x30, daif",
    "mrs x0, tcr_el1",
    "mrs x0, mair_el1",
    "mrs x0, ttbr0_el1",
    "mrs x0, hcr_el2",
    "mrs x0, elr_el2",
    "mrs x0, spsr_el2",

    # -- writing system registers --------------------------------------
    "msr sctlr_el1, x0",
    "msr vbar_el1, x1",
    "msr ttbr0_el1, x2",
    "msr tcr_el1, x3",
    "msr mair_el1, x4",
    "msr elr_el1, x5",
    "msr spsr_el1, x6",
    "msr elr_el2, x7",
    "msr spsr_el2, x8",
    "msr hcr_el2, x9",
    "msr sp_el1, x10",
    "msr sp_el0, x11",
    "msr cpacr_el1, x12",
    "msr elr_el3, x0",
    "msr spsr_el3, x0",
    "msr scr_el3, x0",
    "msr tpidr_el0, x30",
    "msr cntp_ctl_el0, x0",
    "msr cntp_tval_el0, x1",
    "msr cntp_cval_el0, x2",
    "msr cntv_ctl_el0, x3",
    "msr cntkctl_el1, x4",
    "msr cnthctl_el2, x5",
    "msr cntvoff_el2, x6",

    # -- the generic S<op0>_<op1>_C<n>_C<m>_<op2> escape hatch ---------
    "mrs x0, s3_0_c4_c2_2",
    "mrs x1, s3_0_c1_c0_0",
    "msr s3_0_c1_c0_0, x2",

    # -- PSTATE immediate form -----------------------------------------
    "msr daifset, #2",
    "msr daifclr, #2",
    "msr daifset, #15",
    "msr daifclr, #15",
    "msr spsel, #0",
    "msr spsel, #1",

    # -- barriers -------------------------------------------------------
    "isb",
    "dsb sy",
    "dsb ish",
    "dsb ishst",
    "dsb nsh",
    "dmb sy",
    "dmb ish",
    "dmb ishld",

    # -- hints ----------------------------------------------------------
    "nop",
    "wfi",
    "wfe",
    "sev",
    "sevl",
    "yield",

    # -- exception return -----------------------------------------------
    "eret",

    # -- logical immediate: the form boot code cannot do without --------
    "and x0, x0, #3",
    "and w0, w0, #3",
    "and x1, x2, #0xff",
    "orr x0, x0, #0x80000000",
    "orr x0, x0, #1",
    "eor x0, x1, #0xf",
    "ands x0, x1, #0xff",
    "and x0, x0, #0xfffffffffffff000",

    # -- TLB maintenance -------------------------------------------------
    "tlbi vmalle1",
    "tlbi vmalle1is",
    "tlbi alle1",
    "tlbi alle2",
    "tlbi vae1, x0",
    "tlbi aside1, x1",

    # -- cache maintenance -----------------------------------------------
    "ic iallu",
    "ic ialluis",
    "ic ivau, x0",
    "dc civac, x0",
    "dc cvac, x1",
    "dc ivac, x2",
    "dc zva, x3",
    "dc cisw, x4",
]

# `adr` carries a relocation, so only the non-relocated word is comparable.
# Assembling against a local label the assembler can resolve itself would
# compare a *resolved* encoding against our unresolved one and always differ.
ADR_CASES = [
    ("adr x0, sym", 0x10000000),
    ("adr x30, sym", 0x1000001E),
]


def gnu_encode(text):
    """Assemble one instruction with GNU as and return its four bytes."""
    with tempfile.TemporaryDirectory() as d:
        s = os.path.join(d, "t.s")
        o = os.path.join(d, "t.o")
        b = os.path.join(d, "t.bin")
        with open(s, "w") as f:
            f.write(".text\n" + text + "\n")
        r = subprocess.run([AS, "-o", o, s], capture_output=True, text=True)
        if r.returncode != 0:
            return None, r.stderr.strip()
        r = subprocess.run([OBJCOPY, "-O", "binary", "-j", ".text", o, b],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None, r.stderr.strip()
        with open(b, "rb") as f:
            return f.read()[:4], ""


def our_encode(text):
    parts = text.split(None, 1)
    mnem = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    return rasm_arm64.encode_line(mnem, rest)


def main():
    for tool in (AS, OBJCOPY):
        r = subprocess.run(["which", tool], capture_output=True)
        if r.returncode != 0:
            print("SKIP: %s not installed" % tool)
            return 0

    npass = nfail = 0
    for text in CASES:
        want, err = gnu_encode(text)
        if want is None:
            print("  SKIP  %-28s GNU as rejected it: %s" % (text, err))
            continue
        try:
            got, _ = our_encode(text)
        except Exception as e:
            print("  FAIL  %-28s rasm raised %s: %s"
                  % (text, type(e).__name__, e))
            nfail += 1
            continue
        if bytes(got) != bytes(want):
            print("  FAIL  %-28s ours=%s gnu=%s"
                  % (text, bytes(got).hex(), bytes(want).hex()))
            nfail += 1
        else:
            npass += 1

    for text, want_word in ADR_CASES:
        try:
            got, relocs = our_encode(text)
        except Exception as e:
            print("  FAIL  %-28s rasm raised %s: %s"
                  % (text, type(e).__name__, e))
            nfail += 1
            continue
        want = want_word.to_bytes(4, "little")
        if bytes(got) != want:
            print("  FAIL  %-28s ours=%s want=%s"
                  % (text, bytes(got).hex(), want.hex()))
            nfail += 1
        elif not relocs or relocs[0].kind != "adr_prel_lo21":
            print("  FAIL  %-28s wrong/absent relocation" % text)
            nfail += 1
        else:
            npass += 1

    print("\narm64 system instructions: %d pass, %d fail" % (npass, nfail))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
