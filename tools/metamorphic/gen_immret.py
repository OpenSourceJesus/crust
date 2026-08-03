#!/usr/bin/env python3
"""bench_immret.s -- the metamorphic return done right, per the register-jump +
immediate-patch design.

The first benchmark used `jmp QWORD PTR [slot]`, whose 8-byte load cannot be
store-forwarded from a narrow store -- which is why the 1/2-byte writes stalled.
The right shape is a REGISTER-indirect jump: patch the address into the
immediate of a `mov edx, imm32` that sits just before `jmp rdx`. No load at the
jump, so nothing to forward, and the write can be as narrow as the address
needs (2 bytes here, since the image loads low).

That still leaves one store into the instruction stream per call. The real win
is that when the compiler can see the return sites are loop-invariant -- the
whole call chain is fixed across the loop -- it hoists every immediate patch to
*before* the loop. Then the loop body has no store into code at all: each
`mov edx, imm ; jmp rdx` runs a constant, BTB-predicted register jump.

Four lowerings in a 2x2, plus the call/ret reference:

              per-call write            hoisted write (once, pre-loop)
  memory      SLOT_PC  jmp [slot]       SLOT_HO  jmp [slot], slot preset
  immediate   IMM_PC   patch+jmp rdx    IMM_HO   patch once, jmp rdx

Leaf work is identical (eax = x*3+1); the jump register is edx so it never
clobbers the result in eax. Off-page slots for the memory variants; the
immediate variants mprotect their code page RWX so the patch can land.
"""
import sys

N = 60_000_000


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


def timed(tag, pre, body_call):
    # pre: one-time setup emitted INSIDE timing (hoisted patches count as cost,
    # but amortised over N so they vanish); body_call: per-iteration sequence.
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
""".format(tag=tag, pre=pre, body=body_call, N=N,
           prnt=call_printf4("fmt_" + tag, "r13", "r15", "rbp"))


def main():
    out = []
    a = out.append
    a("\t.intel_syntax noprefix")

    a("\t.section .data")
    a("fmt_D:\n\t.byte " + s_bytes(
        "D       call / ret                 : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_SPC:\n\t.byte " + s_bytes(
        "SLOT_PC jmp[slot], write per call  : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_SHO:\n\t.byte " + s_bytes(
        "SLOT_HO jmp[slot], write hoisted   : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_IPC:\n\t.byte " + s_bytes(
        "IMM_PC  jmp rdx,  patch per call   : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_IHO:\n\t.byte " + s_bytes(
        "IMM_HO  jmp rdx,  patch hoisted    : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_ok:\n\t.byte " + s_bytes("all variants agree (acc=%ld)\n"))
    a("fmt_bad:\n\t.byte " + s_bytes("MISMATCH ref=%ld got=%ld (%ld)\n"))
    for t in ("D", "SPC", "SHO", "IPC", "IHO"):
        a("acc_%s:\n\t.quad 0" % t)
    a("\t.align 64")
    a("slotPC:\n\t.quad 0")
    a("\t.align 64")
    a("slotHO:\n\t.quad 0")
    a("\t.align 64\n\t.zero 64")

    a("\t.section .text")
    a("\t.global main")

    def leaf_common():
        a("\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1")

    # call/ret leaf
    a("\t.align 64\nWd:")
    leaf_common(); a("\tret")
    # slot leaves (result eax, jump via memory)
    a("\t.align 64\nWspc:")
    leaf_common(); a("\tjmp QWORD PTR [rip + slotPC]")
    a("\t.align 64\nWsho:")
    leaf_common(); a("\tjmp QWORD PTR [rip + slotHO]")
    # immediate leaves: compute, then a patchable `mov edx, imm32`, then jmp rdx.
    # Keep the body packed -- no alignment padding in the execution path.
    a("\t.align 64\nWipc:")
    leaf_common()
    a("ipc_site:\n\tmov edx, 0\n\tjmp rdx")
    a("\t.align 64\nWiho:")
    leaf_common()
    a("iho_site:\n\tmov edx, 0\n\tjmp rdx")

    a("main:")
    a("\tpush rbx\n\tpush rbp\n\tpush r12\n\tpush r13\n\tpush r14\n\tpush r15")
    a("\tsub rsp, 8")
    # make the immediate leaves' pages writable
    a("\tlea rax, [rip + Wipc]\n\tand rax, -4096\n\tmov rdi, rax")
    a("\tmov rsi, 0x3000\n\tmov edx, 7\n\tcall mprotect")

    # D
    a(timed("D", "\tnop", "\tcall Wd"))

    # SLOT_PC: write slot every call, jmp [slot]
    bodySPC = ("\tlea r11, [rip + .Lret_SPC]\n"
               "\tmov QWORD PTR [rip + slotPC], r11\n"
               "\tjmp Wspc")
    a(timed("SPC", "\tnop", bodySPC))

    # SLOT_HO: write slot once before the loop, jmp [slot] in the loop
    preSHO = ("\tlea r11, [rip + .Lret_SHO]\n"
              "\tmov QWORD PTR [rip + slotHO], r11")
    a(timed("SHO", preSHO, "\tjmp Wsho"))

    # IMM_PC: patch the mov-immediate every call, jmp rdx
    bodyIPC = ("\tlea rax, [rip + .Lret_IPC]\n"
               "\tlea r10, [rip + ipc_site]\n"
               "\tmov WORD PTR [r10+1], ax\n"
               "\tjmp Wipc")
    a(timed("IPC", "\tnop", bodyIPC))

    # IMM_HO: patch the mov-immediate once before the loop, jmp rdx in the loop
    preIHO = ("\tlea rax, [rip + .Lret_IHO]\n"
              "\tlea r10, [rip + iho_site]\n"
              "\tmov WORD PTR [r10+1], ax")
    a(timed("IHO", preIHO, "\tjmp Wiho"))

    # correctness
    def check(tag):
        a("\tmov rax, QWORD PTR [rip + acc_D]")
        a("\tcmp rax, QWORD PTR [rip + acc_%s]\n\tjne .Lbad" % tag)
    for t in ("SPC", "SHO", "IPC", "IHO"):
        check(t)
    a(call_printf4("fmt_ok", "QWORD PTR [rip + acc_D]",
                   "QWORD PTR [rip + acc_D]", "QWORD PTR [rip + acc_D]"))
    a("\tjmp .Ldone")
    a(".Lbad:")
    a(call_printf4("fmt_bad", "QWORD PTR [rip + acc_D]",
                   "QWORD PTR [rip + acc_IHO]", "QWORD PTR [rip + acc_IPC]"))
    a(".Ldone:")

    a("\tadd rsp, 8")
    a("\tpop r15\n\tpop r14\n\tpop r13\n\tpop r12\n\tpop rbp\n\tpop rbx")
    a("\tmov eax, 0\n\tret")

    path = sys.argv[1] if len(sys.argv) > 1 else "bench_immret.s"
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    print("wrote", path, "(N=%d)" % N)


if __name__ == "__main__":
    main()
