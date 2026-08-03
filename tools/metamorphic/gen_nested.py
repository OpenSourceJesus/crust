#!/usr/bin/env python3
"""bench_nested.s -- the loop-invariant call chain A -> B -> C -> D, and the
compiler optimization of hoisting every metamorphic return patch into A's
preamble.

Only D does real work (eax = x*3+1); B and C are pass-throughs, A accumulates.
So the checked accumulator is the same 92129705088 as the other benchmarks.

Four lowerings of the depth-4 chain:

  NCALL   ordinary call/ret at every level. Balanced, so the return-stack
          buffer predicts every ret -- but each level still pushes/pops a
          return address through the stack.

  NSRET   stackless entry (push retaddr; jmp) with ordinary ret. Every ret
          pops a stale RSB entry (no matching call), so all three returns per
          iteration mispredict.

  NMETA_PC  metamorphic returns, but each level patches its `mov edx,imm`
          immediate on every call -- a store into the instruction stream at
          three sites per iteration -> SMC machine clear each time.

  NMETA_HO  metamorphic returns with EVERY patch hoisted into A's preamble
          (done once, before the loop, because the whole chain is loop
          invariant). The loop body is then pure register-indirect jumps to
          constants: no stack traffic, no memory load, no per-iteration store.

NMETA_HO is the one to watch against NCALL: same predicted control flow, but
without the per-level return-address memory traffic that call/ret carries.
"""
import os, sys
N = int(os.environ.get("METAMORPHIC_N", "60000000"))

def s_bytes(text):
    return ",".join(str(b) for b in (text.encode() + b"\x00"))


def call_printf4(fmt_label, r, i, f):
    return """
	mov rax, {f}
	push rax
	mov rax, {i}
	push rax
	mov rax, {r}
	push rax
	lea rax, [rip + {fmt}]
	push rax
	mov r11, rsp
	xor eax, eax
	call printf
	add rsp, 32""".format(fmt=fmt_label, r=r, i=i, f=f)


def timed(tag, pre, body):
    return """
	// ===== variant {tag} =====
{pre}
	.byte 0x0f,0xae,0xe8
	.byte 0x0f,0x31
	shl rdx, 32
	or  rax, rdx
	mov r14, rax
	mov rbx, 0
	mov r12, 0
.Lloop_{tag}:
	mov edi, ebx
	and edi, 1023
{body}
.Lret_{tag}:
	add r12, rax
	add rbx, 1
	cmp rbx, {N}
	jl .Lloop_{tag}
	.byte 0x0f,0xae,0xe8
	.byte 0x0f,0x31
	shl rdx, 32
	or  rax, rdx
	sub rax, r14
	mov r13, rax
	mov rax, r13
	mov rcx, 100
	mul rcx
	mov rcx, {N}
	div rcx
	xor rdx, rdx
	mov rcx, 100
	div rcx
	mov r15, rax
	mov rbp, rdx
	mov QWORD PTR [rip + acc_{tag}], r12
{prnt}
""".format(tag=tag, pre=pre, body=body, N=N,
           prnt=call_printf4("fmt_" + tag, "r13", "r15", "rbp"))


def main():
    out = []
    a = out.append
    a("\t.intel_syntax noprefix")

    a("\t.section .data")
    a("fmt_NCALL:\n\t.byte " + s_bytes(
        "NCALL    call/ret x4  (RSB-balanced)     : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_NSRET:\n\t.byte " + s_bytes(
        "NSRET    push;jmp / ret (RSB desynced)   : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_NMPC:\n\t.byte " + s_bytes(
        "NMETA_PC jmp rdx, patch per call         : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_NMHO:\n\t.byte " + s_bytes(
        "NMETA_HO jmp rdx, patches hoisted to A   : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_ok:\n\t.byte " + s_bytes("all variants agree (acc=%ld)\n"))
    a("fmt_bad:\n\t.byte " + s_bytes("MISMATCH ref=%ld ho=%ld pc=%ld\n"))
    for t in ("NCALL", "NSRET", "NMPC", "NMHO"):
        a("acc_%s:\n\t.quad 0" % t)

    a("\t.section .text")
    a("\t.global main")

    # ---- NCALL chain: ordinary call/ret ----
    a("\t.align 64\nDc:\n\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1\n\tret")
    a("\t.align 64\nCc:\n\tcall Dc\n\tret")
    a("\t.align 64\nBc:\n\tcall Cc\n\tret")

    # ---- NSRET chain: stackless entry (push;jmp), ordinary ret ----
    a("\t.align 64\nDs:\n\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1\n\tret")
    a("\t.align 64\nCs:")
    a("\tlea r11, [rip + .Cs_resume]\n\tpush r11\n\tjmp Ds")
    a(".Cs_resume:\n\tret")
    a("\t.align 64\nBs:")
    a("\tlea r11, [rip + .Bs_resume]\n\tpush r11\n\tjmp Cs")
    a(".Bs_resume:\n\tret")

    # ---- metamorphic chain (shared by PC and HO): each level ends with a
    #      patchable `mov edx,imm ; jmp rdx`. Pass-throughs preserve eax. ----
    a("\t.align 64\nDm:\n\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1")
    a("dm_site:\n\tmov edx, 0\n\tjmp rdx")
    a("\t.align 64\nCm:\n\tjmp Dm")
    a(".Cm_resume:")
    a("cm_site:\n\tmov edx, 0\n\tjmp rdx")
    a("\t.align 64\nBm:\n\tjmp Cm")
    a(".Bm_resume:")
    a("bm_site:\n\tmov edx, 0\n\tjmp rdx")

    a("main:")
    a("\tpush rbx\n\tpush rbp\n\tpush r12\n\tpush r13\n\tpush r14\n\tpush r15")
    a("\tsub rsp, 8")
    # writable code pages for the metamorphic sites
    a("\tlea rax, [rip + Dm]\n\tand rax, -4096\n\tmov rdi, rax")
    a("\tmov rsi, 0x4000\n\tmov edx, 7\n\tcall mprotect")

    # NCALL
    a(timed("NCALL", "\tnop", "\tcall Bc"))

    # NSRET: enter B stackless, returns bubble up via ret (RSB desynced)
    bodyNSRET = ("\tlea r11, [rip + .Lret_NSRET]\n"
                 "\tpush r11\n"
                 "\tjmp Bs")
    a(timed("NSRET", "\tnop", bodyNSRET))

    # NMETA_PC: patch all three sites every iteration, then run the chain
    preNMPC = "\tnop"
    bodyNMPC = (
        "\tlea rax, [rip + .Cm_resume]\n\tlea r10, [rip + dm_site]\n\tmov DWORD PTR [r10+1], eax\n"
        "\tlea rax, [rip + .Bm_resume]\n\tlea r10, [rip + cm_site]\n\tmov DWORD PTR [r10+1], eax\n"
        "\tlea rax, [rip + .Lret_NMPC]\n\tlea r10, [rip + bm_site]\n\tmov DWORD PTR [r10+1], eax\n"
        "\tjmp Bm")
    a(timed("NMPC", preNMPC, bodyNMPC))

    # NMETA_HO: patch all three sites ONCE in the preamble; loop is pure jumps
    preNMHO = (
        "\tlea rax, [rip + .Cm_resume]\n\tlea r10, [rip + dm_site]\n\tmov DWORD PTR [r10+1], eax\n"
        "\tlea rax, [rip + .Bm_resume]\n\tlea r10, [rip + cm_site]\n\tmov DWORD PTR [r10+1], eax\n"
        "\tlea rax, [rip + .Lret_NMHO]\n\tlea r10, [rip + bm_site]\n\tmov DWORD PTR [r10+1], eax")
    a(timed("NMHO", preNMHO, "\tjmp Bm"))

    # correctness
    a("\tmov rax, QWORD PTR [rip + acc_NCALL]")
    a("\tcmp rax, QWORD PTR [rip + acc_NSRET]\n\tjne .Lbad")
    a("\tcmp rax, QWORD PTR [rip + acc_NMPC]\n\tjne .Lbad")
    a("\tcmp rax, QWORD PTR [rip + acc_NMHO]\n\tjne .Lbad")
    a(call_printf4("fmt_ok", "QWORD PTR [rip + acc_NCALL]",
                   "QWORD PTR [rip + acc_NCALL]", "QWORD PTR [rip + acc_NCALL]"))
    a("\tjmp .Ldone")
    a(".Lbad:")
    a(call_printf4("fmt_bad", "QWORD PTR [rip + acc_NCALL]",
                   "QWORD PTR [rip + acc_NMHO]", "QWORD PTR [rip + acc_NMPC]"))
    a(".Ldone:")

    a("\tadd rsp, 8")
    a("\tpop r15\n\tpop r14\n\tpop r13\n\tpop r12\n\tpop rbp\n\tpop rbx")
    a("\tmov eax, 0\n\tret")

    path = sys.argv[1] if len(sys.argv) > 1 else "bench_nested.s"
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    print("wrote", path, "(N=%d)" % N)


if __name__ == "__main__":
    main()
