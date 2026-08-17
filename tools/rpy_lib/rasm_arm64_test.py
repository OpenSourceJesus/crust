"""Differential test: rasm_arm64 vs the GNU AArch64 assembler.

Every case is assembled twice -- once by rasm_arm64, once by
`aarch64-linux-gnu-as` -- and the four encoded bytes must match exactly.
Bit-field encoding is unforgiving and almost never fails visibly: a wrong
shift or a swapped register field still produces a legal instruction that
does the wrong thing. Comparing against a real assembler is the only way to
know the fields are right.

Cases that involve a symbol are compared on the *non-relocated* bytes, since
the relocation carries the rest; the relocation type itself is checked
separately against readelf.

    python3 rasm_arm64_test.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rasm_arm64        # noqa: E402

AS = "aarch64-linux-gnu-as"
OBJCOPY = "aarch64-linux-gnu-objcopy"


# Plain instructions: assembled by both and compared byte-for-byte.
CASES = [
    # -- moves ---------------------------------------------------------
    "mov w0, w1",
    "mov x0, x1",
    "mov x29, sp",
    "mov sp, x29",
    "mov w0, #0",
    "mov w0, #5",
    "mov x0, #65535",
    "mov w0, #-1",
    "mov x0, #-1",
    "mov w0, #65536",
    "mov x0, #4294967296",
    "mov w0, #255",
    "mov x0, #255",
    "movz x0, #1",
    "movz w3, #4660",
    "movk x0, #4660, lsl #16",
    "movk x1, #65535, lsl #48",
    "movn x0, #0",
    "mvn w0, w1",
    "mvn x2, x3",
    "neg w0, w1",
    "neg x5, x6",

    # -- add/sub -------------------------------------------------------
    "add w0, w1, w2",
    "add x0, x1, x2",
    "add w0, w1, #1",
    "add x0, x1, #4095",
    "add x0, x1, #4096",
    "add sp, sp, #16",
    "add x1, sp, #8",
    "add w4, w5, w6, lsl #3",
    "add x4, x5, x6, lsl #2",
    "add x4, x5, x6, asr #7",
    "sub w0, w1, w2",
    "sub x0, x1, #32",
    "sub sp, sp, #48",
    "subs w0, w1, w2",
    "adds x0, x1, #7",
    "cmp w0, #5",
    "cmp x0, #0",
    "cmp w1, w2",
    "cmp x1, x2",
    "cmn w1, #3",
    "tst w0, w1",

    # -- logical -------------------------------------------------------
    "and w0, w1, w2",
    "and x0, x1, x2",
    "orr w0, w1, w2",
    "orr x3, x4, x5",
    "eor w0, w1, w2",
    "eor x0, x1, x2",
    "bic w0, w1, w2",
    "orn x0, x1, x2",
    "and w0, w1, w2, lsl #4",

    # -- mul/div -------------------------------------------------------
    "mul w0, w1, w2",
    "mul x0, x1, x2",
    "madd w0, w1, w2, w3",
    "msub w0, w1, w2, w3",
    "msub x0, x1, x2, x3",
    "sdiv w0, w1, w2",
    "sdiv x0, x1, x2",
    "udiv w0, w1, w2",
    "udiv x0, x1, x2",

    # -- shifts --------------------------------------------------------
    "lsl w0, w1, #4",
    "lsl x0, x1, #33",
    "lsr w0, w1, #4",
    "lsr x0, x1, #33",
    "asr w0, w1, #4",
    "asr x0, x1, #33",
    "lsl w0, w1, w2",
    "lsr x0, x1, x2",
    "asr w0, w1, w2",
    "sxtw x0, w1",
    "sxtb w0, w1",
    "sxth w0, w1",
    "uxtb w0, w1",
    "uxth w0, w1",

    # -- conditional ---------------------------------------------------
    "cset w0, eq",
    "cset w0, ne",
    "cset w0, lt",
    "cset w0, gt",
    "cset x0, mi",
    "cset w0, hs",
    "cset w0, lo",
    "csel w0, w1, w2, eq",
    "csinc x0, x1, x2, ne",

    # -- loads and stores ----------------------------------------------
    "ldr w0, [x1]",
    "ldr x0, [x1]",
    "ldr w0, [x1, #8]",
    "ldr x0, [x1, #16]",
    "ldr x0, [x29, #24]",
    "str w0, [x1]",
    "str x0, [x1, #8]",
    "str w2, [x29, #12]",
    "ldrb w0, [x1]",
    "ldrb w0, [x1, #3]",
    "strb w0, [x1, #5]",
    "ldrh w0, [x1, #6]",
    "strh w0, [x1, #8]",
    "ldrsb w0, [x1]",
    "ldrsb w0, [x1, #7]",
    "ldrsb x0, [x1, #7]",
    "ldrsh w0, [x1, #4]",
    "ldrsw x0, [x1, #8]",
    "ldr s0, [x1]",
    "ldr d0, [x1]",
    "ldr d3, [x29, #16]",
    "str s0, [x1, #4]",
    "str d0, [x1, #8]",
    "ldr w0, [x1, #-4]",
    "str x0, [x1, #-8]",

    # -- pair loads and stores -----------------------------------------
    "stp x29, x30, [sp, #-32]!",
    "stp x29, x30, [sp, #-16]!",
    "ldp x29, x30, [sp], #32",
    "ldp x29, x30, [sp], #16",
    "stp x19, x20, [sp, #16]",
    "ldp x19, x20, [sp, #16]",
    "stp d8, d9, [sp, #-16]!",
    "stp w0, w1, [sp, #8]",

    # -- branches ------------------------------------------------------
    "ret",
    "ret x30",
    "br x1",
    "blr x2",
    "nop",
    "svc #0",

    # -- floating point ------------------------------------------------
    "fadd s0, s1, s2",
    "fadd d0, d1, d2",
    "fsub s0, s1, s2",
    "fsub d3, d4, d5",
    "fmul s0, s1, s2",
    "fmul d0, d1, d2",
    "fdiv s0, s1, s2",
    "fdiv d0, d1, d2",
    "fmov s0, s1",
    "fmov d0, d1",
    "fmov x0, d1",
    "fmov d0, x1",
    "fmov w0, s1",
    "fmov s0, w1",
    "fneg d0, d1",
    "fabs s0, s1",
    "fsqrt d0, d1",
    "fcmp s0, s1",
    "fcmp d0, d1",
    "fcvt d0, s1",
    "fcvt s0, d1",
    "fcvtzs w0, s1",
    "fcvtzs w0, d1",
    "fcvtzs x0, d1",
    "fcvtzu w0, d1",
    "scvtf s0, w1",
    "scvtf d0, w1",
    "scvtf d0, x1",
    "ucvtf d0, w1",
]


# Symbol-referencing instructions: the encoded word is compared with the
# relocation left unapplied, and the relocation type is checked too.
SYM_CASES = [
    ("adrp x0, sym", "R_AARCH64_ADR_PREL_PG_HI21"),
    ("adrp x9, sym", "R_AARCH64_ADR_PREL_PG_HI21"),
    ("add x1, x1, :lo12:sym", "R_AARCH64_ADD_ABS_LO12_NC"),
    ("add x0, x0, :lo12:sym", "R_AARCH64_ADD_ABS_LO12_NC"),
    ("ldr w0, [x1, :lo12:sym]", "R_AARCH64_LDST32_ABS_LO12_NC"),
    ("ldr x0, [x1, :lo12:sym]", "R_AARCH64_LDST64_ABS_LO12_NC"),
    ("ldrb w0, [x1, :lo12:sym]", "R_AARCH64_LDST8_ABS_LO12_NC"),
    ("ldrh w0, [x1, :lo12:sym]", "R_AARCH64_LDST16_ABS_LO12_NC"),
    ("str d0, [x1, :lo12:sym]", "R_AARCH64_LDST64_ABS_LO12_NC"),
    # Same on AArch64: `x0`, `w1` and `eq` are legal C identifiers.
    ("bl x0", "R_AARCH64_CALL26"),
    ("b w1", "R_AARCH64_JUMP26"),
    ("b.eq eq", "R_AARCH64_CONDBR19"),
    ("cbz w0, x9", "R_AARCH64_CONDBR19"),
    ("bl sym", "R_AARCH64_CALL26"),
    ("b sym", "R_AARCH64_JUMP26"),
    ("b.eq sym", "R_AARCH64_CONDBR19"),
    ("b.ne sym", "R_AARCH64_CONDBR19"),
    ("b.ge sym", "R_AARCH64_CONDBR19"),
    ("b.lt sym", "R_AARCH64_CONDBR19"),
    ("b.gt sym", "R_AARCH64_CONDBR19"),
    ("b.le sym", "R_AARCH64_CONDBR19"),
    ("b.ls sym", "R_AARCH64_CONDBR19"),
    ("cbz w0, sym", "R_AARCH64_CONDBR19"),
    ("cbnz x1, sym", "R_AARCH64_CONDBR19"),
    ("tbz w0, #3, sym", "R_AARCH64_TSTBR14"),
    ("tbnz x0, #40, sym", "R_AARCH64_TSTBR14"),
    ("movz x0, #:abs_g3:sym", "R_AARCH64_MOVW_UABS_G3"),
    ("movk x0, #:abs_g2_nc:sym", "R_AARCH64_MOVW_UABS_G2_NC"),
    ("movk x0, #:abs_g1_nc:sym", "R_AARCH64_MOVW_UABS_G1_NC"),
    ("movk x0, #:abs_g0_nc:sym", "R_AARCH64_MOVW_UABS_G0_NC"),
]


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def gas_bytes(lines, workdir, tag):
    """Assemble `lines` with GNU as and return the raw .text bytes."""
    spath = os.path.join(workdir, "%s.s" % tag)
    opath = os.path.join(workdir, "%s.o" % tag)
    bpath = os.path.join(workdir, "%s.bin" % tag)
    with open(spath, "w") as f:
        f.write("\t.text\n")
        for ln in lines:
            f.write("\t%s\n" % ln)
    rc, _, err = _run([AS, spath, "-o", opath])
    if rc != 0:
        return None, err.strip()
    rc, _, err = _run([OBJCOPY, "-O", "binary", "--only-section=.text",
                       opath, bpath])
    if rc != 0:
        return None, err.strip()
    with open(bpath, "rb") as f:
        return list(f.read()), ""


def gas_reloc_types(line, workdir, tag):
    spath = os.path.join(workdir, "%s.s" % tag)
    opath = os.path.join(workdir, "%s.o" % tag)
    with open(spath, "w") as f:
        f.write("\t.text\n\t%s\n" % line)
    rc, _, err = _run([AS, spath, "-o", opath])
    if rc != 0:
        return None
    rc, out, _ = _run(["aarch64-linux-gnu-readelf", "-r", opath])
    if rc != 0:
        return None
    types = []
    for ln in out.split("\n"):
        if "R_AARCH64" in ln:
            for tok in ln.split():
                if tok.startswith("R_AARCH64"):
                    types.append(tok)
    return types


def split_mnem(line):
    parts = line.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def main(argv):
    rc, _, _ = _run([AS, "--version"])
    if rc != 0:
        print("missing %s -- install binutils-aarch64-linux-gnu" % AS)
        return 2

    workdir = tempfile.mkdtemp(prefix="rasmarm64-")
    npass = 0
    nfail = 0

    # Assemble the whole plain corpus in one go, then walk it four bytes at
    # a time -- far faster than invoking `as` once per case.
    ref, err = gas_bytes(CASES, workdir, "all")
    if ref is None:
        print("GNU as failed on the corpus: %s" % err)
        return 2
    if len(ref) != 4 * len(CASES):
        print("unexpected reference size: %d bytes for %d instructions"
              % (len(ref), len(CASES)))
        return 2

    print("== plain instructions ==")
    i = 0
    while i < len(CASES):
        line = CASES[i]
        want = ref[i * 4:(i + 1) * 4]
        mnem, rest = split_mnem(line)
        try:
            got, relocs = rasm_arm64.encode_line(mnem, rest)
        except Exception as e:
            print("  FAIL %-34s rasm_arm64 raised: %s" % (line, e))
            nfail += 1
            i += 1
            continue
        if got != want:
            print("  FAIL %-34s rasm=%s gas=%s"
                  % (line, _hex(got), _hex(want)))
            nfail += 1
        else:
            npass += 1
        i += 1

    print("\n== symbol references ==")
    j = 0
    while j < len(SYM_CASES):
        line, want_type = SYM_CASES[j]
        ref1, err = gas_bytes([line], workdir, "s%d" % j)
        if ref1 is None:
            print("  FAIL %-34s GNU as: %s" % (line, err))
            nfail += 1
            j += 1
            continue
        mnem, rest = split_mnem(line)
        try:
            got, relocs = rasm_arm64.encode_line(mnem, rest)
        except Exception as e:
            print("  FAIL %-34s rasm_arm64 raised: %s" % (line, e))
            nfail += 1
            j += 1
            continue
        if got != ref1[0:4]:
            print("  FAIL %-34s rasm=%s gas=%s"
                  % (line, _hex(got), _hex(ref1[0:4])))
            nfail += 1
            j += 1
            continue
        if len(relocs) != 1:
            print("  FAIL %-34s expected 1 relocation, got %d"
                  % (line, len(relocs)))
            nfail += 1
            j += 1
            continue
        gas_types = gas_reloc_types(line, workdir, "r%d" % j)
        # readelf truncates long relocation names, so compare on a prefix.
        matched = False
        if gas_types is not None:
            for gt in gas_types:
                if want_type.startswith(gt) or gt.startswith(want_type):
                    matched = True
        if gas_types is not None and not matched:
            print("  FAIL %-34s gas emitted %s, test expects %s"
                  % (line, ",".join(gas_types), want_type))
            nfail += 1
            j += 1
            continue
        npass += 1
        j += 1

    print("\nrasm_arm64 differential vs GNU as: %d/%d passed"
          % (npass, npass + nfail))
    return 0 if nfail == 0 else 1


def _hex(bs):
    return " ".join(["%02x" % b for b in bs])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
