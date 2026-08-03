#!/usr/bin/env python3
"""Emit bench_metaret_variants.s: four ways for a leaf to return, each doing
identical work (eax = x*3+1) in a tight N-iteration loop, cycle-timed with
rdtsc. The only thing that differs is the return mechanism:

  A  metamorphic, return slot in the SAME cache line as the leaf's code,
     full 8-byte write per call   -- this is today's -fmetamorphic layout,
     the store hits the executing instruction line => SMC machine-clear.
  B  metamorphic, slot moved to .data (a different page from any code),
     full 8-byte write per call    -- no SMC, but a wide store.
  C  metamorphic, slot in .data, ONE-byte write per call. Correct only
     because the whole image loads low (base 0x1000, every code address
     < 0x4000) so a return address fits in the low 2 bytes and the top 7
     bytes of the slot never change; we pre-seed the slot once and then
     patch only the low byte each call.
  D  ordinary call/ret               -- the reference.

Variant A needs its slot writable while sharing a page with code; rlink puts
text in an R+X segment, so main mprotects the text pages to RWX at startup.
That is the honest cost of the SMC-adjacent design and is exactly what the
real -fmetamorphic .mtext section asks the loader for.
"""
import os, sys
N = int(os.environ.get("METAMORPHIC_N", "60000000"))


def s_bytes(text):
    return ",".join(str(b) for b in (text.encode() + b"\x00"))


# printf(fmt, a, b, c) under the ShivyCX all-stack variadic ABI: push args so
# fmt is lowest, r11 -> fmt, al = 0. Caller cleans 4*8 bytes.
def call_printf4(fmt_label, rsrc, isrc, fsrc):
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
	add rsp, 32""".format(fmt=fmt_label, r=rsrc, i=isrc, f=fsrc)


# One timed variant. body_call is the per-iteration call sequence, which must
# leave the leaf result in eax and fall through to a label we can accumulate
# from. name_label is the .data format string.
def timed(tag, setup, body_call):
    return """
	// ===== variant {tag} =====
{setup}
	.byte 0x0f,0xae,0xe8            // lfence
	.byte 0x0f,0x31                // rdtsc -> edx:eax
	shl rdx, 32
	or  rax, rdx
	mov r14, rax                   // start tsc
	mov rbx, 0                     // i
	mov r12, 0                     // acc
.Lloop_{tag}:
	mov edi, ebx
	and edi, 1023
{body}
.Lret_{tag}:
	add r12, rax                   // accumulate (eax zero-extended into rax)
	add rbx, 1
	cmp rbx, {N}
	jl .Lloop_{tag}
	.byte 0x0f,0xae,0xe8           // lfence
	.byte 0x0f,0x31               // rdtsc
	shl rdx, 32
	or  rax, rdx
	sub rax, r14                   // elapsed cycles
	mov r13, rax                   // save elapsed
	// cyc/call *100 = elapsed*100/N  -> int part r15, frac rbp
	mov rax, r13
	mov rcx, 100
	mul rcx                        // rdx:rax = elapsed*100
	mov rcx, {N}
	div rcx                        // rax = elapsed*100/N
	xor rdx, rdx
	mov rcx, 100
	div rcx                        // rax=int part, rdx=frac
	mov r15, rax
	mov rbp, rdx
	mov QWORD PTR [rip + acc_{tag}], r12
{prnt}
""".format(tag=tag, setup=setup, body=body_call, N=N,
           prnt=call_printf4("fmt_" + tag, "r13", "r15", "rbp"))


def main():
    out = []
    a = out.append
    a("\t.intel_syntax noprefix")

    # -------- data: format strings + off-page slots for B and C --------
    a("\t.section .data")
    a("fmt_A:\n\t.byte " + s_bytes(
        "A  slot in code line, 8B write : %12ld cyc  %6ld.%02ld cyc/call\n"))
    a("fmt_B:\n\t.byte " + s_bytes(
        "B  slot off-page,     8B write : %12ld cyc  %6ld.%02ld cyc/call\n"))
    a("fmt_C:\n\t.byte " + s_bytes(
        "C  slot off-page,     1B write : %12ld cyc  %6ld.%02ld cyc/call\n"))
    a("fmt_C2:\n\t.byte " + s_bytes(
        "C2 slot off-page,     2B write : %12ld cyc  %6ld.%02ld cyc/call\n"))
    a("fmt_D:\n\t.byte " + s_bytes(
        "D  ordinary call / ret         : %12ld cyc  %6ld.%02ld cyc/call\n"))
    a("fmt_ok:\n\t.byte " + s_bytes("all variants agree (acc=%ld)\n"))
    a("fmt_bad:\n\t.byte " + s_bytes("MISMATCH: A=%ld B=%ld C=%ld\n"))
    for t in ("A", "B", "C", "C2", "D"):
        a("acc_%s:\n\t.quad 0" % t)
    # cache-line-separated slots in writable data, far from any code page
    a("\t.align 64")
    a("slotB:\n\t.quad 0")
    a("\t.align 64")
    a("slotC:\n\t.quad 0")
    a("\t.align 64")
    a("slotC2:\n\t.quad 0")
    a("\t.align 64\n\t.zero 64")

    # -------- text: four leaves --------
    a("\t.section .text")
    a("\t.global main")

    # Variant A leaf: slot at the start of a 64B line, leaf code right after,
    # so a store to slotA lands in the same cache line the CPU is fetching
    # leafA from -> self-modifying-code machine clear.
    a("\t.align 64")
    a("slotA:\n\t.quad 0")
    a("leafA:")
    a("\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1")
    a("\tjmp QWORD PTR [rip + slotA]")

    a("\t.align 64")
    a("leafB:")
    a("\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1")
    a("\tjmp QWORD PTR [rip + slotB]")

    a("\t.align 64")
    a("leafC:")
    a("\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1")
    a("\tjmp QWORD PTR [rip + slotC]")

    a("\t.align 64")
    a("leafC2:")
    a("\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1")
    a("\tjmp QWORD PTR [rip + slotC2]")

    a("\t.align 64")
    a("leafD:")
    a("\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1")
    a("\tret")

    # -------- main --------
    a("main:")
    a("\tpush rbx\n\tpush rbp\n\tpush r12\n\tpush r13\n\tpush r14\n\tpush r15")
    a("\tsub rsp, 8")               # 6 pushes keep %16==8; +8 -> 16-aligned

    # make the page holding slotA/leafA RWX so variant A can write its
    # in-line slot; derive it from the symbol so it works at any load base.
    a("\tlea rax, [rip + slotA]")
    a("\tand rax, -4096")
    a("\tmov rdi, rax")
    a("\tmov rsi, 0x2000")          # slotA/leafA span at most two pages
    a("\tmov edx, 7")               # PROT_READ|WRITE|EXEC
    a("\tcall mprotect")

    # A: slot in code line, full 8B write each call
    setupA = "\tlea r11, [rip + .Lret_A]\n\tmov QWORD PTR [rip + slotA], r11"
    bodyA = ("\tlea r11, [rip + .Lret_A]\n"
             "\tmov QWORD PTR [rip + slotA], r11\n"
             "\tjmp leafA")
    a(timed("A", setupA, bodyA))

    # B: slot off-page, full 8B write each call
    setupB = "\tlea r11, [rip + .Lret_B]\n\tmov QWORD PTR [rip + slotB], r11"
    bodyB = ("\tlea r11, [rip + .Lret_B]\n"
             "\tmov QWORD PTR [rip + slotB], r11\n"
             "\tjmp leafB")
    a(timed("B", setupB, bodyB))

    # C: slot off-page, seed full pointer once, then write ONLY the low byte
    setupC = ("\tlea r11, [rip + .Lret_C]\n"
              "\tmov QWORD PTR [rip + slotC], r11")
    bodyC = ("\tlea r11, [rip + .Lret_C]\n"
             "\tmov BYTE PTR [rip + slotC], r11b\n"
             "\tjmp leafC")
    a(timed("C", setupC, bodyC))

    # C2: slot off-page, seed once, then write ONLY the low 2 bytes
    setupC2 = ("\tlea r11, [rip + .Lret_C2]\n"
               "\tmov QWORD PTR [rip + slotC2], r11")
    bodyC2 = ("\tlea r11, [rip + .Lret_C2]\n"
              "\tmov WORD PTR [rip + slotC2], r11w\n"
              "\tjmp leafC2")
    a(timed("C2", setupC2, bodyC2))

    # D: ordinary call/ret
    setupD = "\tnop"
    bodyD = "\tcall leafD"
    a(timed("D", setupD, bodyD))

    # correctness: every variant must have computed the same accumulator
    a("\tmov rax, QWORD PTR [rip + acc_A]")
    a("\tcmp rax, QWORD PTR [rip + acc_B]\n\tjne .Lbad")
    a("\tcmp rax, QWORD PTR [rip + acc_C]\n\tjne .Lbad")
    a("\tcmp rax, QWORD PTR [rip + acc_C2]\n\tjne .Lbad")
    a("\tcmp rax, QWORD PTR [rip + acc_D]\n\tjne .Lbad")
    a(call_printf4("fmt_ok", "QWORD PTR [rip + acc_A]",
                   "QWORD PTR [rip + acc_A]", "QWORD PTR [rip + acc_A]"))
    a("\tjmp .Ldone")
    a(".Lbad:")
    a(call_printf4("fmt_bad", "QWORD PTR [rip + acc_A]",
                   "QWORD PTR [rip + acc_B]", "QWORD PTR [rip + acc_C]"))
    a(".Ldone:")

    a("\tadd rsp, 8")
    a("\tpop r15\n\tpop r14\n\tpop r13\n\tpop r12\n\tpop rbp\n\tpop rbx")
    a("\tmov eax, 0")
    a("\tret")

    text = "\n".join(out) + "\n"
    path = sys.argv[1] if len(sys.argv) > 1 else "bench_metaret_variants.s"
    with open(path, "w") as f:
        f.write(text)
    print("wrote", path, "(N=%d)" % N)


if __name__ == "__main__":
    main()
