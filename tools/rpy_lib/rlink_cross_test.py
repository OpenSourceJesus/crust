"""Differential test: rlink as a cross-architecture linker.

For each program below we assemble with the GNU cross assembler, then link the
*same* object twice -- once with GNU `ld`, once with rlink -- run both under
qemu, and require identical exit codes. Using `ld` as the oracle is what makes
this meaningful: a relocation applied with the wrong shift or mask usually
still produces a runnable binary, just one that jumps somewhere wrong, so
"it linked" proves very little on its own.

The corpus is organised by relocation type rather than by language feature,
because relocations are what this layer gets wrong. Every type rlink claims to
support should appear in at least one program here.

    python3 rlink_cross_test.py            # both architectures
    python3 rlink_cross_test.py arm64      # just one

Requires: binutils-aarch64-linux-gnu, binutils-riscv64-linux-gnu, qemu-user.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RLINK = os.path.join(HERE, "rlink.py")


# Each entry: (name, asm source, expected relocation types it should exercise).
# The reloc list is checked against readelf, so a program that stops covering
# what it claims fails loudly instead of silently testing nothing.
ARM64_PROGS = [
    ("call26", """
	.global _start
	.text
_start:	mov	x0, #41
	bl	helper
	mov	x8, #93
	svc	#0
	.global helper
helper:	add	x0, x0, #1
	ret
""", ["R_AARCH64_CALL26"]),

    ("adrp_add_lo12", """
	.global _start
	.text
_start:	adrp	x1, val
	add	x1, x1, :lo12:val
	ldr	w0, [x1]
	mov	x8, #93
	svc	#0
	.data
	.align 2
val:	.int 77
""", ["R_AARCH64_ADR_PREL_PG_HI21", "R_AARCH64_ADD_ABS_LO12_NC"]),

    ("ldst_lo12", """
	.global _start
	.text
_start:	adrp	x1, g8
	ldrb	w2, [x1, :lo12:g8]
	adrp	x1, g32
	ldr	w3, [x1, :lo12:g32]
	adrp	x1, g64
	ldr	x4, [x1, :lo12:g64]
	add	w0, w2, w3
	add	w0, w0, w4
	mov	x8, #93
	svc	#0
	.data
g8:	.byte 3
	.align 2
g32:	.int 10
	.align 3
g64:	.quad 20
""", ["R_AARCH64_LDST8_ABS_LO12_NC", "R_AARCH64_LDST32_ABS_LO12_NC",
      "R_AARCH64_LDST64_ABS_LO12_NC"]),

    ("abs64_data", """
	.global _start
	.text
_start:	adrp	x1, fnptr
	add	x1, x1, :lo12:fnptr
	ldr	x2, [x1]
	mov	x0, #10
	blr	x2
	mov	x8, #93
	svc	#0
	.global helper
helper:	add	x0, x0, #5
	ret
	.data
	.align 3
fnptr:	.quad helper
""", ["R_AARCH64_ABS64"]),

    ("condbr19_jump26", """
	.global _start
	.text
_start:	mov	w0, #5
	cmp	w0, #5
	b.eq	taken
	mov	w0, #99
	b	out
	.global taken
taken:	mov	w0, #33
	b	out
	.global out
out:	mov	x8, #93
	svc	#0
""", ["R_AARCH64_CONDBR19", "R_AARCH64_JUMP26"]),

    ("movw_uabs", """
	.global _start
	.text
_start:	movz	x1, #:abs_g3:val
	movk	x1, #:abs_g2_nc:val
	movk	x1, #:abs_g1_nc:val
	movk	x1, #:abs_g0_nc:val
	ldr	w0, [x1]
	mov	x8, #93
	svc	#0
	.data
	.align 2
val:	.int 88
""", ["R_AARCH64_MOVW_UABS_G0_NC", "R_AARCH64_MOVW_UABS_G3"]),

    ("bss_and_common", """
	.global _start
	.text
_start:	adrp	x1, counter
	add	x1, x1, :lo12:counter
	mov	w2, #7
	str	w2, [x1]
	ldr	w0, [x1]
	mov	x8, #93
	svc	#0
	.bss
	.align 2
counter: .zero 4
""", ["R_AARCH64_ADR_PREL_PG_HI21"]),

    ("cross_section_call", """
	.global _start
	.text
_start:	mov	x0, #20
	bl	f1
	bl	f2
	mov	x8, #93
	svc	#0
	.section .text.f1,"ax",@progbits
	.global f1
f1:	add	x0, x0, #1
	ret
	.section .text.f2,"ax",@progbits
	.global f2
f2:	add	x0, x0, #2
	ret
""", ["R_AARCH64_CALL26"]),
]


# Real RISC-V crt code sets gp before anything else, and the linker may relax
# PC-relative sequences into gp-relative ones -- so a test program without
# this prologue can be miscompared purely because gp held garbage. Kept
# inside .option norelax because the `la` that loads gp cannot itself be
# relaxed to be gp-relative.
GP_INIT = """.option push
.option norelax
	la	gp, __global_pointer$
.option pop
"""

RISCV_PROGS = [
    ("call", """
	.global _start
	.text
_start:
""" + GP_INIT + """	li	a0, 41
	call	helper
	li	a7, 93
	ecall
	.global helper
helper:	addi	a0, a0, 1
	ret
""", ["R_RISCV_CALL_PLT"]),

    ("pcrel_hi_lo", """
	.global _start
	.text
_start:
""" + GP_INIT + """	lla	t0, val
	lw	a0, 0(t0)
	li	a7, 93
	ecall
	.data
	.align 2
val:	.word 77
""", ["R_RISCV_PCREL_HI20", "R_RISCV_PCREL_LO12_I"]),

    ("pcrel_store", """
	.global _start
	.text
_start:
""" + GP_INIT + """	li	t1, 55
	lla	t0, slot
	sw	t1, 0(t0)
	lw	a0, 0(t0)
	li	a7, 93
	ecall
	.bss
	.align 2
slot:	.zero 4
""", ["R_RISCV_PCREL_HI20"]),

    ("branch", """
	.global _start
	.text
_start:
""" + GP_INIT + """	li	a0, 5
	li	t0, 5
	beq	a0, t0, taken
	li	a0, 99
	j	out
	.global taken
taken:	li	a0, 33
	.global out
out:	li	a7, 93
	ecall
""", ["R_RISCV_BRANCH"]),

    ("data_ptr", """
	.global _start
	.text
_start:
""" + GP_INIT + """	lla	t0, fnptr
	ld	t1, 0(t0)
	li	a0, 10
	jalr	t1
	li	a7, 93
	ecall
	.global helper
helper:	addi	a0, a0, 5
	ret
	.data
	.align 3
fnptr:	.dword helper
""", ["R_RISCV_64"]),

    ("cross_section_call", """
	.global _start
	.text
_start:
""" + GP_INIT + """	li	a0, 20
	call	f1
	call	f2
	li	a7, 93
	ecall
	.section .text.f1,"ax",@progbits
	.global f1
f1:	addi	a0, a0, 1
	ret
	.section .text.f2,"ax",@progbits
	.global f2
f2:	addi	a0, a0, 2
	ret
""", ["R_RISCV_CALL_PLT"]),
]


# --------------------------------------------------------------------------
# Offset sweeps
# --------------------------------------------------------------------------
# The two subtlest bugs in this layer only appear at particular displacements,
# so a fixed corpus of small programs sails past them:
#
#   * adrp truncates *both* its own PC and the target to a 4KB page boundary.
#     Forgetting to truncate the PC is invisible whenever the two happen to
#     sit at the same offset within their pages.
#   * The RISC-V auipc+jalr pair splits a 32-bit displacement, and jalr
#     *sign-extends* its 12-bit half. So when the low half is >= 0x800 the
#     high half must be incremented to compensate. Dropping that borrow is
#     invisible unless the displacement actually lands in the top half of a
#     4KB window.
#
# Both are caught by sweeping the displacement across a page rather than by
# adding more hand-written programs, so these are generated.
_SWEEP_PADS = [0, 0x7F8, 0x800, 0x808, 0xFF8, 0x1000, 0x1808, 0x2004]


def _arm64_sweep():
    progs = []
    for pad in _SWEEP_PADS:
        progs.append(("sweep_adrp_%04x" % pad, """
	.global _start
	.text
_start:	adrp	x1, val
	add	x1, x1, :lo12:val
	ldr	w0, [x1]
	mov	x8, #93
	svc	#0
	.space %d
	.data
	.space %d
	.align 2
val:	.int 77
""" % (pad, pad), ["R_AARCH64_ADR_PREL_PG_HI21"]))
    return progs


def _riscv_sweep():
    progs = []
    for pad in _SWEEP_PADS:
        # Padding between the call site and the callee walks the displacement
        # through the range where the jalr sign-extension borrow matters.
        progs.append(("sweep_call_%04x" % pad, """
	.global _start
	.text
_start:
""" + GP_INIT + """	li	a0, 41
	call	helper
	li	a7, 93
	ecall
	.space %d
	.global helper
helper:	addi	a0, a0, 1
	ret
""" % pad, ["R_RISCV_CALL_PLT"]))
    # For auipc+addi the borrow lives in the *low 12 bits* of the code-to-data
    # displacement, so padding .text and .data by the same amount moves both
    # ends together and never reaches it -- exactly the hole that let a broken
    # PCREL_HI20 pass unnoticed. Pad only .data, in steps that walk the low
    # half across a full 4KB window, so some cases must land at or above 0x800
    # whatever the fixed part of the displacement happens to be.
    i = 0
    while i < 16:
        dpad = i * 0x200
        progs.append(("sweep_pcrel_%04x" % dpad, """
	.global _start
	.text
_start:
""" + GP_INIT + """	lla	t0, val
	lw	a0, 0(t0)
	li	a7, 93
	ecall
	.data
	.space %d
	.align 2
val:	.word 77
""" % dpad, ["R_RISCV_PCREL_HI20", "R_RISCV_PCREL_LO12_I"]))
        i += 1
    return progs


ARM64_PROGS = ARM64_PROGS + _arm64_sweep()
RISCV_PROGS = RISCV_PROGS + _riscv_sweep()


ARCHES = {
    "arm64": {
        "as": "aarch64-linux-gnu-as",
        "ld": "aarch64-linux-gnu-ld",
        "readelf": "aarch64-linux-gnu-readelf",
        "qemu": "qemu-aarch64",
        "progs": ARM64_PROGS,
    },
    "riscv64": {
        "as": "riscv64-linux-gnu-as",
        "ld": "riscv64-linux-gnu-ld",
        "readelf": "riscv64-linux-gnu-readelf",
        "qemu": "qemu-riscv64",
        "progs": RISCV_PROGS,
    },
}


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _have(tool):
    rc, _, _ = _run([tool, "--version"])
    return rc == 0


def test_one(arch, name, src, want_relocs, workdir):
    cfg = ARCHES[arch]
    spath = os.path.join(workdir, "%s_%s.s" % (arch, name))
    opath = os.path.join(workdir, "%s_%s.o" % (arch, name))
    with open(spath, "w") as f:
        f.write(src)

    rc, _, err = _run([cfg["as"], spath, "-o", opath])
    if rc != 0:
        return "ERROR", "assembling failed: %s" % err.strip()[:150]

    # The corpus claims to cover specific relocation types; verify it still
    # does, so a program that quietly stops exercising one is caught.
    rc, out, _ = _run([cfg["readelf"], "-r", opath])
    for want in want_relocs:
        # readelf truncates long type names, so compare on a prefix.
        if want[:17] not in out:
            return "ERROR", "expected relocation %s not present" % want

    ref = os.path.join(workdir, "%s_%s.ref" % (arch, name))
    rc, _, err = _run([cfg["ld"], opath, "-o", ref])
    if rc != 0:
        return "ERROR", "GNU ld failed: %s" % err.strip()[:150]

    mine = os.path.join(workdir, "%s_%s.my" % (arch, name))
    rc, out, err = _run([sys.executable, RLINK, "-o", mine, opath])
    if rc != 0:
        return "FAIL", "rlink failed: %s" % (err.strip() or out.strip())[:150]

    ref_rc, _, _ = _run([cfg["qemu"], ref])
    my_rc, _, _ = _run([cfg["qemu"], mine])
    if ref_rc != my_rc:
        return "FAIL", "exit mismatch: rlink=%d ld=%d" % (my_rc, ref_rc)
    return "PASS", "exit=%d" % my_rc


def main(argv):
    which = argv[1:] if len(argv) > 1 else ["arm64", "riscv64"]
    env = dict(os.environ)
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    os.environ.update(env)

    workdir = tempfile.mkdtemp(prefix="rlinkcross-")
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "ERROR": 0}
    for arch in which:
        cfg = ARCHES.get(arch)
        if cfg is None:
            print("unknown arch: %s" % arch)
            return 2
        missing = [t for t in (cfg["as"], cfg["ld"], cfg["qemu"])
                   if not _have(t)]
        if missing:
            print("\n== %s ==  SKIP (missing: %s)"
                  % (arch, ", ".join(missing)))
            counts["SKIP"] += len(cfg["progs"])
            continue
        print("\n== %s ==" % arch)
        for name, src, relocs in cfg["progs"]:
            status, detail = test_one(arch, name, src, relocs, workdir)
            counts[status] += 1
            print("  %-5s %-20s %s" % (status, name, detail))

    print("\nrlink cross-arch: %d pass, %d fail, %d skip, %d error"
          % (counts["PASS"], counts["FAIL"], counts["SKIP"], counts["ERROR"]))
    return 0 if counts["FAIL"] == 0 and counts["ERROR"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
