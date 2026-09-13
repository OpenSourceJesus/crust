"""Differential test for rasm's encoder against the GNU assembler.

Every instruction is assembled two ways -- with rasm and with `as` (reference,
via objdump) -- and must produce byte-identical machine code. All cases are
assembled in a single `as` invocation (one label per line) to keep the test
fast. Register / immediate / memory operands are checked directly; symbolic
operands (jumps, calls, RIP-relative loads) are checked for the correct opcode
bytes, zero placeholder, and relocation record.
"""
import subprocess
import tempfile
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rasm


# Cases with only reg/imm/mem operands: full byte-for-byte comparison with `as`.
CASES = [
    "mov rax, rbx", "mov eax, edi", "mov r8, r9", "mov r12, rbx",
    "mov ecx, r8d", "mov r8d, edi", "mov rsp, rbp", "mov rbp, rsp",
    "mov al, bl", "mov ax, bx",
    "mov eax, 5", "mov rax, 5", "mov edi, 3", "mov esi, 4", "mov r8d, 7",
    "mov rax, -1", "mov ecx, 1000000",
    "mov QWORD PTR [rbp-8], r12", "mov QWORD PTR [rbp-16], rbx",
    "mov DWORD PTR [rbp-4], eax", "mov eax, DWORD PTR [rbp-4]",
    "mov r12, QWORD PTR [rbp-8]", "mov rax, QWORD PTR [rsp+16]",
    "mov QWORD PTR [rax], rbx", "mov rbx, QWORD PTR [r12]",
    "mov rax, QWORD PTR [r13+0]", "mov eax, DWORD PTR [rcx+256]",
    "mov DWORD PTR [rbp-16], 5", "mov QWORD PTR [rbp-8], 0",
    "mov eax, DWORD PTR [rax+rcx*4]", "mov DWORD PTR [rax+rcx*4+8], edx",
    "mov rax, QWORD PTR [rbx+rsi*8+256]",
    "lea rax, [rbp-8]", "lea rcx, [rax+rdx*2+4]",
    "push rbp", "pop rbp", "push r12", "pop r13", "push rax",
    "add eax, esi", "add ebx, eax", "sub edi, esi", "sub rsp, rbp",
    "and rsi, rdi", "or eax, ebx", "xor eax, eax", "cmp r12d, r8d",
    "add rax, rcx", "sub rax, rbx",
    "sub rsp, 16", "add rsp, 16", "cmp r12d, 2", "cmp rax, 100",
    "add edi, 1", "sub r12d, 2", "and eax, 255", "cmp esi, 1000",
    "add rax, 128", "sub eax, -1", "cmp eax, 2000000000",
    "add DWORD PTR [rbp-4], 1", "sub QWORD PTR [rbp-8], 16",
    "cmp DWORD PTR [rbp-4], eax",
    "imul edx, ecx", "imul rcx, rsi", "imul eax, edx, 10", "imul rax, rbx, 200",
    "imul eax, 10", "imul ebx, 100", "imul rax, 1000", "imul eax, 5",
    "idiv rcx", "idiv ecx", "cqo", "cdq", "div rbx",
    "sal eax, 2", "shl rax, 4", "sar eax, 1", "shr rcx, 8", "sal eax, 1",
    "movsx eax, al", "movsx rax, eax", "movzx eax, al", "movsx ecx, bx",
    "ret", "leave", "nop",
    "call rax", "jmp rax", "call QWORD PTR [rax]",
    "neg eax", "not rax", "test eax, eax", "test rax, rbx",
    # SSE scalar float
    "movsd xmm0, QWORD PTR [rbp-16]", "movsd QWORD PTR [rbp-8], xmm0",
    "movsd xmm1, xmm0", "movss xmm0, DWORD PTR [rbp-4]",
    "addsd xmm0, xmm1", "subsd xmm0, QWORD PTR [rbp-16]", "mulsd xmm0, xmm3",
    "divsd xmm1, xmm2", "ucomisd xmm0, xmm1", "ucomiss xmm0, xmm1",
    "cvtsi2sd xmm0, edi", "cvtsi2sd xmm0, rax", "cvttsd2si ebx, xmm0",
    "cvttsd2si rax, xmm1", "cvtsd2ss xmm0, xmm1", "cvtss2sd xmm1, xmm0",
    "movq xmm0, rax", "movq rbx, xmm1", "xorps xmm0, xmm0", "pxor xmm2, xmm2",
    "sqrtsd xmm0, xmm1", "movsd xmm8, QWORD PTR [rbp-16]", "addsd xmm9, xmm10",
    # 128-bit unaligned moves (the Windows runtime's setjmp/longjmp)
    "movups XMMWORD PTR [rcx+80], xmm6", "movups xmm15, XMMWORD PTR [rcx+224]",
    "movups XMMWORD PTR [rcx+208], xmm14", "movups xmm7, XMMWORD PTR [rcx+96]",
    # the Win64 stack probe
    "test DWORD PTR [rsp], esp", "mov r11, 1052672", "sub rsp, 4096",
    "sub r11, 4096", "cmp r11, 4096", "sub rsp, r11",
]

# Symbolic cases: (instruction, expected_opcode_prefix_bytes, reloc_pcrel).
# `as` emits the same opcode + zero placeholder + a relocation for an undefined
# symbol; we check our opcode bytes and that we recorded a matching reloc.
SYM_CASES = [
    ("jmp extlabel", ["e9", "00", "00", "00", "00"], True),
    ("call extfunc", ["e8", "00", "00", "00", "00"], True),
    ("je extlabel", ["0f", "84", "00", "00", "00", "00"], True),
    ("jge extlabel", ["0f", "8d", "00", "00", "00", "00"], True),
    ("lea rax, [rip+extsym]", ["48", "8d", "05", "00", "00", "00", "00"], True),
]


def batch_reference(cases):
    src = ".intel_syntax noprefix\n"
    for i, ins in enumerate(cases):
        src += "L%d:\n\t%s\n" % (i, ins)
    f = tempfile.NamedTemporaryFile("w", suffix=".s", delete=False)
    f.write(src)
    f.close()
    obj = f.name + ".o"
    try:
        subprocess.check_call(["as", "--64", "-o", obj, f.name],
                              stderr=subprocess.DEVNULL)
        dis = subprocess.check_output(
            ["objdump", "-d", "-M", "intel", obj]).decode()
    finally:
        for p in (f.name, obj):
            try:
                os.unlink(p)
            except OSError:
                pass
    want = {}
    cur = None
    for line in dis.split("\n"):
        lm = re.match(r"[0-9a-f]+ <L(\d+)>:", line)
        if lm:
            cur = int(lm.group(1))
            want[cur] = []
            continue
        bm = re.match(r"\s+[0-9a-f]+:\s+((?:[0-9a-f]{2} )+)", line)
        if bm and cur is not None:
            want[cur] += bm.group(1).split()
    return want


def main():
    want = batch_reference(CASES)
    npass = 0
    fails = []
    for i, ins in enumerate(CASES):
        try:
            k, m, ops = rasm.parse_line(ins)
            body, _ = rasm.encode(m, ops)
            got = ["%02x" % b for b in body]
        except Exception as e:
            fails.append((ins, "rasm-error: %s" % e, ""))
            continue
        w = want.get(i, [])
        if got == w:
            npass += 1
        else:
            fails.append((ins, " ".join(got), " ".join(w)))

    # symbolic / relocation cases
    for ins, expect, pcrel in SYM_CASES:
        try:
            k, m, ops = rasm.parse_line(ins)
            body, relocs = rasm.encode(m, ops)
            got = ["%02x" % b for b in body]
        except Exception as e:
            fails.append((ins, "rasm-error: %s" % e, ""))
            continue
        ok = (got == expect and len(relocs) == 1 and relocs[0].pcrel == pcrel)
        if ok:
            npass += 1
        else:
            rd = "reloc=%d" % len(relocs)
            fails.append((ins, " ".join(got) + " " + rd, " ".join(expect)))

    total = len(CASES) + len(SYM_CASES)
    print("rasm differential vs GNU as: %d/%d passed" % (npass, total))
    if fails:
        print("\nFAILURES:")
        for ins, got, w in fails:
            print("  %-34s rasm=[%s] as=[%s]" % (ins, got, w))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
