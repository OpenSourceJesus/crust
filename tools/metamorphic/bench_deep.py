#!/usr/bin/env python3
"""bench_deep.py -- sweep call-chain depth to find where a metamorphic return
beats balanced call/ret.

A balanced call/ret is RSB-predicted only while the chain fits in the return
stack buffer (~16-32 entries on current x86). Past that depth the outermost
returns are evicted and mispredict. A monomorphic `jmp rdx` metamorphic return
is predicted by the BTB regardless of depth. So deep enough chains should tip
in metamorphic's favour.

For each depth K we emit two chains of K pass-through levels (only the deepest
does work), time both, and print cyc per outer iteration. Metamorphic patches
are all hoisted into the entry preamble (loop invariant), so the loop body is
pure jumps.

    python3 bench_deep.py 4 8 16 24 32 48 64
"""
import os, re, subprocess, sys, statistics
HERE = os.path.dirname(os.path.abspath(__file__))
N = int(os.environ.get("METAMORPHIC_N", "20000000"))
LINE = re.compile(r"(NCALL|NMHO)\s+(\d+)\s+cyc", re.M)


def s_bytes(text):
    return ",".join(str(b) for b in (text.encode() + b"\x00"))


def printf4(fmt, r, i, f):
    return ("\n\tmov rax, {f}\n\tpush rax\n\tmov rax, {i}\n\tpush rax\n"
            "\tmov rax, {r}\n\tpush rax\n\tlea rax, [rip + {fmt}]\n\tpush rax\n"
            "\tmov r11, rsp\n\txor eax, eax\n\tcall printf\n\tadd rsp, 32"
            ).format(fmt=fmt, r=r, i=i, f=f)


def timed(tag, pre, body):
    return """
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
	mov QWORD PTR [rip + acc_{tag}], r12
{prnt}
""".format(tag=tag, pre=pre, body=body, N=N,
           prnt=printf4("fmt_" + tag, "r13", "r13", "r13"))


def gen(depth):
    o = []
    a = o.append
    a("\t.intel_syntax noprefix")
    a("\t.section .data")
    a("fmt_NCALL:\n\t.byte " + s_bytes("NCALL %ld cyc %ld %ld\n"))
    a("fmt_NMHO:\n\t.byte " + s_bytes("NMHO %ld cyc %ld %ld\n"))
    a("acc_NCALL:\n\t.quad 0\nacc_NMHO:\n\t.quad 0")

    a("\t.section .text")
    a("\t.global main")

    # ---- call/ret chain: Kc_0 (entry) -> ... -> Kc_{depth-1} (work) ----
    a("\t.align 64\nKc_%d:" % (depth - 1))
    a("\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1\n\tret")
    for i in range(depth - 2, -1, -1):
        a("\t.align 64\nKc_%d:\n\tcall Kc_%d\n\tret" % (i, i + 1))

    # ---- metamorphic chain: down-jmps + patchable `mov edx,imm; jmp rdx` ----
    a("\t.align 64\nKm_%d:" % (depth - 1))
    a("\tmov eax, edi\n\timul eax, 3\n\tadd eax, 1")
    a("site_%d:\n\tmov edx, 0\n\tjmp rdx" % (depth - 1))
    for i in range(depth - 2, -1, -1):
        a("\t.align 64\nKm_%d:\n\tjmp Km_%d" % (i, i + 1))
        a(".res_%d:" % i)
        a("site_%d:\n\tmov edx, 0\n\tjmp rdx" % i)

    a("main:")
    a("\tpush rbx\n\tpush rbp\n\tpush r12\n\tpush r13\n\tpush r14\n\tpush r15")
    a("\tsub rsp, 8")
    a("\tlea rax, [rip + Km_%d]\n\tand rax, -4096\n\tmov rdi, rax" % (depth - 1))
    a("\tmov rsi, 0x10000\n\tmov edx, 7\n\tcall mprotect")

    # NCALL
    a(timed("NCALL", "\tnop", "\tcall Kc_0"))

    # NMETA_HO: patch each site once. site_i returns to .res_{i-1}; site_0
    # returns to the loop's .Lret_NMHO.
    pre = []
    for i in range(depth):
        tgt = ".Lret_NMHO" if i == 0 else ".res_%d" % (i - 1)
        pre.append("\tlea rax, [rip + %s]\n\tlea r10, [rip + site_%d]\n"
                   "\tmov WORD PTR [r10+1], ax" % (tgt, i))
    a(timed("NMHO", "\n".join(pre), "\tjmp Km_0"))

    # correctness + report
    a("\tmov rax, QWORD PTR [rip + acc_NCALL]")
    a("\tcmp rax, QWORD PTR [rip + acc_NMHO]\n\tjne .Lbad\n\tjmp .Ldone")
    a(".Lbad:")
    a("\tlea rax, [rip + fmt_bad]\n\tpush rax\n\tmov r11, rsp\n\txor eax, eax\n\tcall printf\n\tadd rsp, 8")
    a(".Ldone:")
    a("\tadd rsp, 8")
    a("\tpop r15\n\tpop r14\n\tpop r13\n\tpop r12\n\tpop rbp\n\tpop rbx")
    a("\tmov eax, 0\n\tret")
    a("\t.section .data\nfmt_bad:\n\t.byte " + s_bytes("MISMATCH\n"))
    return "\n".join(o) + "\n"


def run(depth):
    asm = os.path.join(HERE, "_deep.s")
    binp = os.path.join(HERE, "_deep")
    with open(asm, "w") as f:
        f.write(gen(depth))
    subprocess.run([sys.executable, os.path.join(HERE, "rbuild.py"), asm, binp,
                    "0x1000"], check=True, capture_output=True)
    vals = {}
    for _ in range(5):
        out = subprocess.run([binp], capture_output=True, text=True).stdout
        for tag, cyc in LINE.findall(out):
            vals.setdefault(tag, []).append(int(cyc) / N)
    return statistics.median(vals["NCALL"]), statistics.median(vals["NMHO"])


def main():
    depths = [int(x) for x in sys.argv[1:]] or [4, 8, 16, 24, 32, 48, 64]
    print("depth   call/ret   meta_HO   winner")
    print("-----   --------   -------   ------")
    for d in depths:
        c, m = run(d)
        win = "meta" if m < c else "call/ret"
        print("%5d   %8.2f   %7.2f   %s" % (d, c, m, win))


if __name__ == "__main__":
    main()
