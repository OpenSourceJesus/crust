#!/usr/bin/env python3
"""bench_trampoline.s -- the case where a metamorphic return *wins*.

A normal `call`/`ret` pair is predicted almost perfectly by the CPU's
return-stack buffer (RSB): the `call` pushes the return address onto the RSB,
the `ret` pops it. That is why variant B in the other benchmark only *ties*
call/ret -- it cannot beat a perfectly predicted return.

The RSB only works when calls and returns are balanced. Stackless / trampoline
code (which METAMORPHIC.md calls out as the motivating case) enters a worker by
*jumping* into it, not calling it -- so nothing is pushed onto the RSB. Three
ways to then get back:

  D  call W / ret            -- the balanced reference. RSB-perfect, fast.
                                (kept only to show the predicted-return speed.)
  R  push ret_addr; jmp W / ret
                             -- trampoline entry, ordinary return. The `ret`
                                pops a stale RSB entry (we never `call`ed), so
                                it mispredicts *every* iteration.
  M  mov [slot],ret_addr; jmp W / jmp [slot]
                             -- trampoline entry, metamorphic return. The
                                indirect `jmp [slot]` is predicted by the BTB
                                on its own address; the target is stable, so it
                                predicts and does NOT desync any RSB.

R and M have identical control flow -- enter W by jmp, do the same work, come
back to the same label -- and differ only in the return instruction. So the gap
between them is exactly the RSB-misprediction cost that the metamorphic return
avoids. The slot lives off-page (the fix from the first benchmark) and takes a
full 8-byte write, so no SMC and no store-forwarding stall muddy the result.
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


def timed(tag, body_call):
    return """
	// ===== variant {tag} =====
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
""".format(tag=tag, body=body_call, N=N,
           prnt=call_printf4("fmt_" + tag, "r13", "r15", "rbp"))


def main():
    out = []
    a = out.append
    a("\t.intel_syntax noprefix")

    a("\t.section .data")
    a("fmt_D:\n\t.byte " + s_bytes(
        "D  call / ret         (RSB-balanced)   : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_R:\n\t.byte " + s_bytes(
        "R  push;jmp / ret     (RSB desynced)   : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_M:\n\t.byte " + s_bytes(
        "M  jmp / jmp[slot]    (metamorphic)    : %12ld cyc  %6ld.%02ld /call\n"))
    a("fmt_ok:\n\t.byte " + s_bytes("all variants agree (acc=%ld)\n"))
    a("fmt_bad:\n\t.byte " + s_bytes("MISMATCH D=%ld R=%ld M=%ld\n"))
    for t in ("D", "R", "M"):
        a("acc_%s:\n\t.quad 0" % t)
    a("\t.align 64")
    a("slotM:\n\t.quad 0")
    a("\t.align 64\n\t.zero 64")

    a("\t.section .text")
    a("\t.global main")

    # worker for the call/ret reference
    a("\t.align 64")
    a("Wd:")
    a("\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1")
    a("\tret")
    # worker entered by jmp, returns by ret
    a("\t.align 64")
    a("Wr:")
    a("\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1")
    a("\tret")
    # worker entered by jmp, returns metamorphically
    a("\t.align 64")
    a("Wm:")
    a("\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1")
    a("\tjmp QWORD PTR [rip + slotM]")

    a("main:")
    a("\tpush rbx\n\tpush rbp\n\tpush r12\n\tpush r13\n\tpush r14\n\tpush r15")
    a("\tsub rsp, 8")

    # D: balanced call/ret
    a(timed("D", "\tcall Wd"))

    # R: trampoline entry (push return addr, jmp), ordinary ret
    bodyR = ("\tlea r11, [rip + .Lret_R]\n"
             "\tpush r11\n"
             "\tjmp Wr")
    a(timed("R", bodyR))

    # M: trampoline entry (jmp), metamorphic return
    bodyM = ("\tlea r11, [rip + .Lret_M]\n"
             "\tmov QWORD PTR [rip + slotM], r11\n"
             "\tjmp Wm")
    a(timed("M", bodyM))

    # correctness
    a("\tmov rax, QWORD PTR [rip + acc_D]")
    a("\tcmp rax, QWORD PTR [rip + acc_R]\n\tjne .Lbad")
    a("\tcmp rax, QWORD PTR [rip + acc_M]\n\tjne .Lbad")
    a(call_printf4("fmt_ok", "QWORD PTR [rip + acc_D]",
                   "QWORD PTR [rip + acc_D]", "QWORD PTR [rip + acc_D]"))
    a("\tjmp .Ldone")
    a(".Lbad:")
    a(call_printf4("fmt_bad", "QWORD PTR [rip + acc_D]",
                   "QWORD PTR [rip + acc_R]", "QWORD PTR [rip + acc_M]"))
    a(".Ldone:")

    a("\tadd rsp, 8")
    a("\tpop r15\n\tpop r14\n\tpop r13\n\tpop r12\n\tpop rbp\n\tpop rbx")
    a("\tmov eax, 0\n\tret")

    path = sys.argv[1] if len(sys.argv) > 1 else "bench_trampoline.s"
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    print("wrote", path, "(N=%d)" % N)


if __name__ == "__main__":
    main()
