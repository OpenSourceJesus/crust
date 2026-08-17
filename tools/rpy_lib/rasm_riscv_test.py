"""Differential test: rasm_riscv vs the GNU RISC-V assembler.

Same shape as rasm_arm64_test.py -- every case is assembled by both
rasm_riscv and `riscv64-linux-gnu-as`, and the encoded bytes must match
exactly.

RV64 needs this more than AArch64 did, for two reasons. S-type and B-type
immediates are split across non-adjacent fields and B/J-type reorder the bits
as well, so a wrong shift still yields a legal instruction that branches
somewhere else entirely. And most of what the compiler emits is a
pseudo-instruction whose expansion is a convention, not a spec -- `li` picks
one or two instructions depending on the constant, and the only way to know
we match gas is to ask gas.

    python3 rasm_riscv_test.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rasm_riscv       # noqa: E402

AS = "riscv64-linux-gnu-as"
OBJCOPY = "riscv64-linux-gnu-objcopy"
# -mno-relax keeps gas from rewriting sequences into gp-relative form, so we
# compare the instructions as written rather than gas's relaxed rewrite.
AS_FLAGS = ["-march=rv64imafd", "-mno-relax"]


CASES = [
    # -- pseudo-instructions -------------------------------------------
    "nop",
    "li a0, 0",
    "li a0, 5",
    "li a0, -1",
    "li a0, 2047",
    "li a0, -2048",
    "li a0, 2048",
    "li a0, -2049",
    "li a0, 4096",
    "li a0, 305419896",
    "li t0, -305419896",
    "li a0, 2147483647",
    "li s3, 1000000",
    # 64-bit constants: gas builds these with a recursive shift-and-add
    # sequence, so each is several instructions and the shape has to match.
    "li a0, 4294967296",
    "li a0, 4294967295",
    "li a0, 78187493520",
    "li a0, -78187493520",
    "li a1, 81985529216486895",
    "li a2, -1099511627776",
    "li a3, 1099511627775",
    "li a4, 9223372036854775807",
    "li a5, -9223372036854775808",
    "li a6, 2147483648",
    "li a7, -2147483649",
    "li t0, 68719476736",
    "li t1, 1152921504606846976",
    "mv a0, a1",
    "mv s0, t3",
    "ret",
    "neg a0, a1",
    "negw a0, a1",
    "not a0, a1",
    "seqz a0, a1",
    "snez a0, a1",
    "sltz a0, a1",
    "sgtz a0, a1",
    "sext.w a0, a1",
    "jr t0",
    "jalr t0",

    # -- register-register ---------------------------------------------
    "add a0, a1, a2",
    "sub a0, a1, a2",
    "sll a0, a1, a2",
    "slt a0, a1, a2",
    "sltu a0, a1, a2",
    "xor a0, a1, a2",
    "srl a0, a1, a2",
    "sra a0, a1, a2",
    "or a0, a1, a2",
    "and a0, a1, a2",
    "addw a0, a1, a2",
    "subw a0, a1, a2",
    "sllw a0, a1, a2",
    "srlw a0, a1, a2",
    "sraw a0, a1, a2",
    "mul a0, a1, a2",
    "mulh a0, a1, a2",
    "mulhu a0, a1, a2",
    "div a0, a1, a2",
    "divu a0, a1, a2",
    "rem a0, a1, a2",
    "remu a0, a1, a2",
    "mulw a0, a1, a2",
    "divw a0, a1, a2",
    "divuw a0, a1, a2",
    "remw a0, a1, a2",
    "remuw a0, a1, a2",
    "add x5, x6, x7",
    "add zero, ra, sp",
    "add t6, s11, a7",

    # -- register-immediate --------------------------------------------
    "addi a0, a1, 0",
    "addi a0, a1, 100",
    "addi a0, a1, -100",
    "addi sp, sp, -32",
    "addi a0, a1, 2047",
    "addi a0, a1, -2048",
    "addiw a0, a1, 7",
    "slti a0, a1, 5",
    "sltiu a0, a1, 5",
    "xori a0, a1, -1",
    "ori a0, a1, 15",
    "andi a0, a1, 255",
    "slli a0, a1, 1",
    "slli a0, a1, 31",
    "slli a0, a1, 63",
    "srli a0, a1, 12",
    "srli a0, a1, 63",
    "srai a0, a1, 12",
    "srai a0, a1, 63",
    "slliw a0, a1, 5",
    "srliw a0, a1, 5",
    "sraiw a0, a1, 5",

    # -- loads and stores ----------------------------------------------
    "lb a0, 0(a1)",
    "lh a0, 2(a1)",
    "lw a0, 4(a1)",
    "ld a0, 8(a1)",
    "lbu a0, 1(a1)",
    "lhu a0, 2(a1)",
    "lwu a0, 4(a1)",
    "ld a0, -8(sp)",
    "ld s0, 2040(sp)",
    "ld s0, -2048(sp)",
    "sb a0, 0(a1)",
    "sh a0, 2(a1)",
    "sw a0, 4(a1)",
    "sd a0, 8(a1)",
    "sd ra, -16(sp)",
    "sd s0, 2040(sp)",
    "sd s0, -2048(sp)",
    "flw fa0, 4(a1)",
    "fld fa0, 8(a1)",
    "fsw fa0, 4(a1)",
    "fsd fa0, 8(a1)",

    # -- upper immediates ----------------------------------------------
    "lui a0, 1",
    "lui a0, 1048575",
    "auipc a0, 0",
    "auipc t0, 4096",

    # -- system --------------------------------------------------------
    "ecall",
    "ebreak",

    # -- floating point ------------------------------------------------
    "fadd.s fa0, fa1, fa2",
    "fadd.d fa0, fa1, fa2",
    "fsub.s fa0, fa1, fa2",
    "fsub.d fa0, fa1, fa2",
    "fmul.s fa0, fa1, fa2",
    "fmul.d fa0, fa1, fa2",
    "fdiv.s fa0, fa1, fa2",
    "fdiv.d fa0, fa1, fa2",
    "fsqrt.d fa0, fa1",
    "fmin.d fa0, fa1, fa2",
    "fmax.d fa0, fa1, fa2",
    "fmv.d fa0, fa1",
    "fneg.d fa0, fa1",
    "fabs.d fa0, fa1",
    "fsgnj.d fa0, fa1, fa2",
    "feq.d a0, fa1, fa2",
    "flt.d a0, fa1, fa2",
    "fle.d a0, fa1, fa2",
    "feq.s a0, fa1, fa2",
    "fcvt.w.d a0, fa1",
    "fcvt.w.d a0, fa1, rtz",
    "fcvt.wu.d a0, fa1, rtz",
    "fcvt.l.d a0, fa1, rtz",
    "fcvt.lu.d a0, fa1, rtz",
    "fcvt.w.s a0, fa1, rtz",
    "fcvt.d.l fa0, a1, rne",
    "fcvt.s.w fa0, a1, rtz",
    "fcvt.wu.d a0, fa1",
    "fcvt.l.d a0, fa1",
    "fcvt.lu.d a0, fa1",
    "fcvt.w.s a0, fa1",
    "fcvt.d.w fa0, a1",
    "fcvt.d.wu fa0, a1",
    "fcvt.d.l fa0, a1",
    "fcvt.s.w fa0, a1",
    "fcvt.s.d fa0, fa1",
    "fcvt.d.s fa0, fa1",
    "fmv.x.w a0, fa1",
    "fmv.x.d a0, fa1",
    "fmv.w.x fa0, a1",
    "fmv.d.x fa0, a1",
]


# Symbol-referencing cases: (source line, expected relocation types).
SYM_CASES = [
    # A label may legitimately be spelled like a register: `f2`, `a0` and `ra`
    # are all ordinary C function names, and must not be swallowed by the
    # register parser when they appear in a label position.
    ("call f2", ["R_RISCV_CALL"]),
    ("call a0", ["R_RISCV_CALL"]),
    ("call ra", ["R_RISCV_CALL"]),
    ("j s1", ["R_RISCV_JAL"]),
    ("call sym", ["R_RISCV_CALL"]),
    ("tail sym", ["R_RISCV_CALL"]),
    ("j sym", ["R_RISCV_JAL"]),
    ("jal sym", ["R_RISCV_JAL"]),
    ("jal ra, sym", ["R_RISCV_JAL"]),
    ("lui a0, %hi(sym)", ["R_RISCV_HI20"]),
    ("auipc a0, %pcrel_hi(sym)", ["R_RISCV_PCREL_HI20"]),
    ("addi a0, a0, %lo(sym)", ["R_RISCV_LO12_I"]),
    ("lw a0, %lo(sym)(a1)", ["R_RISCV_LO12_I"]),
    ("sw a0, %lo(sym)(a1)", ["R_RISCV_LO12_S"]),
    ("ld a0, %lo(sym)(a1)", ["R_RISCV_LO12_I"]),
    ("sd a0, %lo(sym)(a1)", ["R_RISCV_LO12_S"]),
]


# Branches need their own treatment. gas cannot know how far away an external
# symbol is, so it relaxes `beq a0,a1,sym` into an inverted branch over a
# `j sym` -- two instructions, and no longer comparable byte-for-byte with the
# single branch we emit. Targeting a label at distance zero keeps gas from
# relaxing, and zero is also the placeholder we leave for the linker, so the
# words line up exactly.
BRANCH_CASES = [
    "beq a0, a1, 1b",
    "bne a0, a1, 1b",
    "blt a0, a1, 1b",
    "bge a0, a1, 1b",
    "bltu a0, a1, 1b",
    "bgeu a0, a1, 1b",
    "bgt a0, a1, 1b",
    "ble a0, a1, 1b",
    "bgtu a0, a1, 1b",
    "bleu a0, a1, 1b",
    "beqz a0, 1b",
    "bnez a0, 1b",
    "blez a0, 1b",
    "bgez a0, 1b",
    "bltz a0, 1b",
    "bgtz a0, 1b",
    "j 1b",
    "jal 1b",
]


# Branch and jump immediates are split across non-adjacent fields *and*
# reordered, and the tests above cannot see any of that: every branch there
# targets distance zero, so all the immediate bits are zero and misplacing
# them is invisible. (Both a swapped B-type bit 11 and a dropped J-type
# imm[19:12] survived the corpus until this section existed.)
#
# We never emit a resolved branch offset -- the linker fills it in -- so
# there is nothing to compare end-to-end here. Instead this exercises the
# field-splitting helpers directly, against a branch gas *did* resolve
# locally, at distances chosen to light up every bit of each field.
BRANCH_DISTS = [4, 8, 16, 32, 64, 128, 256, 512, 1024, 2044,
                -4, -8, -16, -64, -256, -1024, -2048]
JUMP_DISTS = [4, 8, 256, 4096, 8192, 65536, 262144, 1048572,
              -4, -256, -4096, -65536, -1048576]


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def gas_bytes(lines, workdir, tag):
    spath = os.path.join(workdir, "%s.s" % tag)
    opath = os.path.join(workdir, "%s.o" % tag)
    bpath = os.path.join(workdir, "%s.bin" % tag)
    with open(spath, "w") as f:
        f.write("\t.text\n")
        for ln in lines:
            f.write("\t%s\n" % ln)
    rc, _, err = _run([AS] + AS_FLAGS + [spath, "-o", opath])
    if rc != 0:
        return None, err.strip()
    rc, _, err = _run([OBJCOPY, "-O", "binary", "--only-section=.text",
                       opath, bpath])
    if rc != 0:
        return None, err.strip()
    with open(bpath, "rb") as f:
        return list(f.read()), ""


def gas_relocs(line, workdir, tag):
    spath = os.path.join(workdir, "%s.s" % tag)
    opath = os.path.join(workdir, "%s.o" % tag)
    with open(spath, "w") as f:
        f.write("\t.text\n\t%s\n" % line)
    rc, _, _ = _run([AS] + AS_FLAGS + [spath, "-o", opath])
    if rc != 0:
        return None
    rc, out, _ = _run(["riscv64-linux-gnu-readelf", "-r", opath])
    if rc != 0:
        return None
    types = []
    for ln in out.split("\n"):
        for tok in ln.split():
            if tok.startswith("R_RISCV"):
                types.append(tok)
    return types


def _hex(bs):
    return " ".join(["%02x" % b for b in bs])


def split_mnem(line):
    parts = line.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def main(argv):
    rc, _, _ = _run([AS, "--version"])
    if rc != 0:
        print("missing %s -- install binutils-riscv64-linux-gnu" % AS)
        return 2

    workdir = tempfile.mkdtemp(prefix="rasmriscv-")
    npass = 0
    nfail = 0

    # Cases expand to a variable number of instructions (li, call), so each
    # is assembled on its own rather than as one block walked four bytes at
    # a time.
    print("== plain instructions ==")
    i = 0
    while i < len(CASES):
        line = CASES[i]
        want, err = gas_bytes([line], workdir, "c%d" % i)
        if want is None:
            print("  FAIL %-30s GNU as: %s" % (line, err[:90]))
            nfail += 1
            i += 1
            continue
        mnem, rest = split_mnem(line)
        try:
            got, relocs = rasm_riscv.encode_line(mnem, rest)
        except Exception as e:
            print("  FAIL %-30s rasm_riscv raised: %s" % (line, str(e)[:80]))
            nfail += 1
            i += 1
            continue
        if got != want:
            print("  FAIL %-30s rasm=%s gas=%s"
                  % (line, _hex(got), _hex(want)))
            nfail += 1
        else:
            npass += 1
        i += 1

    print("\n== branches (local target, distance zero) ==")
    k = 0
    while k < len(BRANCH_CASES):
        line = BRANCH_CASES[k]
        want, err = gas_bytes(["1:", line], workdir, "b%d" % k)
        if want is None:
            print("  FAIL %-30s GNU as: %s" % (line, err[:90]))
            nfail += 1
            k += 1
            continue
        mnem, rest = split_mnem(line)
        try:
            got, relocs = rasm_riscv.encode_line(mnem, rest)
        except Exception as e:
            print("  FAIL %-30s rasm_riscv raised: %s" % (line, str(e)[:80]))
            nfail += 1
            k += 1
            continue
        if got != want:
            print("  FAIL %-30s rasm=%s gas=%s"
                  % (line, _hex(got), _hex(want)))
            nfail += 1
        elif len(relocs) != 1:
            print("  FAIL %-30s expected one relocation, got %d"
                  % (line, len(relocs)))
            nfail += 1
        else:
            npass += 1
        k += 1

    print("\n== immediate field splitting ==")
    for dist in BRANCH_DISTS:
        # Forward: pad between the branch and its target. Backward: put the
        # target first and pad after it.
        if dist > 0:
            lines = ["beq a0, a1, 9f", ".space %d" % (dist - 4), "9:"]
        else:
            lines = ["9:", ".space %d" % (-dist), "beq a0, a1, 9b"]
        want, err = gas_bytes(lines, workdir, "bd%d" % (dist & 0xFFFFF))
        if want is None:
            print("  FAIL branch dist=%-8d GNU as: %s" % (dist, err[:70]))
            nfail += 1
            continue
        off = 0 if dist > 0 else len(want) - 4
        wanted = want[off:off + 4]
        got = rasm_riscv._bytes_of(
            rasm_riscv._b_type(rasm_riscv.OP_BRANCH, 0, 10, 11, dist))
        if got != wanted:
            print("  FAIL branch dist=%-8d rasm=%s gas=%s"
                  % (dist, _hex(got), _hex(wanted)))
            nfail += 1
        else:
            npass += 1

    for dist in JUMP_DISTS:
        if dist > 0:
            lines = ["jal ra, 9f", ".space %d" % (dist - 4), "9:"]
        else:
            lines = ["9:", ".space %d" % (-dist), "jal ra, 9b"]
        want, err = gas_bytes(lines, workdir, "jd%d" % (dist & 0x1FFFFF))
        if want is None:
            print("  FAIL jal dist=%-8d GNU as: %s" % (dist, err[:70]))
            nfail += 1
            continue
        off = 0 if dist > 0 else len(want) - 4
        wanted = want[off:off + 4]
        got = rasm_riscv._bytes_of(
            rasm_riscv._j_type(rasm_riscv.OP_JAL, 1, dist))
        if got != wanted:
            print("  FAIL jal dist=%-8d rasm=%s gas=%s"
                  % (dist, _hex(got), _hex(wanted)))
            nfail += 1
        else:
            npass += 1

    print("\n== symbol references ==")
    j = 0
    while j < len(SYM_CASES):
        line, want_types = SYM_CASES[j]
        want, err = gas_bytes([line], workdir, "s%d" % j)
        if want is None:
            print("  FAIL %-30s GNU as: %s" % (line, err[:90]))
            nfail += 1
            j += 1
            continue
        mnem, rest = split_mnem(line)
        try:
            got, relocs = rasm_riscv.encode_line(mnem, rest)
        except Exception as e:
            print("  FAIL %-30s rasm_riscv raised: %s" % (line, str(e)[:80]))
            nfail += 1
            j += 1
            continue
        if got != want:
            print("  FAIL %-30s rasm=%s gas=%s"
                  % (line, _hex(got), _hex(want)))
            nfail += 1
            j += 1
            continue
        if len(relocs) < 1:
            print("  FAIL %-30s no relocation emitted" % line)
            nfail += 1
            j += 1
            continue
        gtypes = gas_relocs(line, workdir, "g%d" % j)
        if gtypes is not None:
            ok = False
            for want_t in want_types:
                for gt in gtypes:
                    if gt.startswith(want_t[:14]) or want_t.startswith(gt):
                        ok = True
            if not ok:
                print("  FAIL %-30s gas emitted %s, expected %s"
                      % (line, ",".join(gtypes), ",".join(want_types)))
                nfail += 1
                j += 1
                continue
        npass += 1
        j += 1

    print("\nrasm_riscv differential vs GNU as: %d/%d passed"
          % (npass, npass + nfail))
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
