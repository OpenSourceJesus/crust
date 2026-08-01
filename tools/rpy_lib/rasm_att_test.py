"""Differential test: AT&T-syntax parsing and the extended instruction set.

Each case is a single AT&T-syntax instruction. It is assembled twice -- once by
rasm (parse_att_line + encode) and once by GNU `as` -- and the two byte strings
must match exactly. This covers both the new AT&T front end and the
instructions added for the linker/runtime work (syscall, setcc, cmovcc,
inc/dec, xchg, string ops, movabs, ...).
"""
import os
import sys
import subprocess
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rasm

CASES = [
    # --- moves ---
    "movq %rsp, %rax",
    "movq %rax, -8(%rbp)",
    "movl $5, -8(%rbp)",
    "movl -12(%rbp), %eax",
    "movb $1, (%rdi)",
    "movw %ax, 6(%rdx)",
    "movq (%rax,%rbx,4), %rcx",
    "movq -16(%rbp,%r12,8), %r9",
    "movabsq $0x1122334455667788, %rax",
    "movabsq $0x1122334455667788, %r11",
    "movq %r8, %r15",
    "movb %sil, %dil",
    # --- lea / rip-relative ---
    "leaq -8(%rbp), %rax",
    "leaq (%rdi,%rsi,2), %rdx",
    # --- alu ---
    "addq %rdx, %rcx",
    "addl $1, %eax",
    "addl $128, %ebx",
    "subq $32, %rsp",
    "andl $0xff, %edx",
    "orq %r10, %r11",
    "xorl %eax, %eax",
    "cmpq $0, %rdi",
    "cmpl %esi, %edi",
    "testb %al, %al",
    "testq %rax, %rax",
    "imulq %rbx, %rax",
    "imull $3, %eax, %edx",
    "negq %rax",
    "notl %ecx",
    "idivq %rcx",
    "divl %esi",
    # --- shifts ---
    "shlq $3, %rax",
    "sarl $31, %edx",
    "shrq %cl, %rbx",
    # --- inc/dec/xchg/bswap ---
    "incq %rax",
    "incl -4(%rbp)",
    "decq %r12",
    "decb (%rdi)",
    "xchg %rax, %rbx",
    "bswap %eax",
    # --- setcc / cmovcc ---
    "sete %al",
    "setne %dl",
    "setl %sil",
    "setg (%rdi)",
    "cmove %rbx, %rax",
    "cmovnel %edx, %eax",
    "cmovgq %r9, %r10",
    # --- extending moves ---
    "movzbl %al, %ecx",
    "movzbq (%rdi), %rax",
    "movsbl %dil, %eax",
    "movswq %ax, %rbx",
    "movslq %edi, %rsi",
    "movzwl 4(%rdx), %ecx",
    # --- stack / control ---
    "pushq %rbp",
    "pushq %r13",
    "popq %rbx",
    "call *%rax",
    "jmp *%rdx",
    "callq *(%rbx)",
    # --- no-operand and system ---
    "ret",
    "leave",
    "nop",
    "cltq",
    "cqto",
    "syscall",
    "hlt",
    "int3",
    "ud2",
    "cld",
    "endbr64",
    "rdtsc",
    "cpuid",
    "pause",
    "mfence",
    # --- string ops with prefixes ---
    "rep movsb",
    "rep movsq",
    "rep stosb",
    "rep stosq",
    "repne scasb",
    # --- interrupts ---
    "int $0x80",
    # --- SSE in AT&T form ---
    "movsd %xmm0, %xmm1",
    "addsd %xmm2, %xmm3",
    "movsd -8(%rbp), %xmm0",
    "cvtsi2sdq %rax, %xmm0",
    "ucomisd %xmm1, %xmm0",
    "pxor %xmm4, %xmm4",
    "movq %xmm0, %rax",
]

# a few mnemonics rasm normalises but whose AT&T suffix `as` also accepts
SUFFIX_FIXUPS = {"cvtsi2sdq": ("cvtsi2sd", 64)}


def gas_encode(line, workdir):
    src = os.path.join(workdir, "t.s")
    obj = os.path.join(workdir, "t.o")
    with open(src, "w") as f:
        f.write(".text\n" + line + "\n")
    subprocess.check_call(["as", "--64", "-o", obj, src])
    out = subprocess.check_output(["objcopy", "-O", "binary",
                                   "--only-section=.text", obj,
                                   os.path.join(workdir, "t.bin")])
    with open(os.path.join(workdir, "t.bin"), "rb") as f:
        return list(f.read())


def rasm_encode(line):
    kind, mnem, ops = rasm.parse_att_line(line)
    if kind != "insn":
        raise AssertionError("not an instruction: %s" % line)
    if mnem in SUFFIX_FIXUPS:
        fix = SUFFIX_FIXUPS[mnem]
        mnem = fix[0]
    body, relocs = rasm.encode(mnem, ops)
    return body


def hexs(bs):
    return " ".join("%02x" % b for b in bs)


def main():
    workdir = tempfile.mkdtemp(prefix="rasm_att_")
    passed = 0
    failed = 0
    for line in CASES:
        try:
            want = gas_encode(line, workdir)
        except subprocess.CalledProcessError:
            print("  SKIP (as rejected) %s" % line)
            continue
        try:
            got = rasm_encode(line)
        except Exception as e:
            print("  FAIL %-34s rasm error: %s" % (line, e))
            failed += 1
            continue
        if got == want:
            passed += 1
        else:
            failed += 1
            print("  FAIL %-34s rasm=%s  as=%s" % (line, hexs(got), hexs(want)))
    print("\nrasm AT&T/extended differential: %d/%d passed"
          % (passed, passed + failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
