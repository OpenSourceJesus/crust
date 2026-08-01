"""Differential test for bare-metal assembly: .code32, mode switching, and the
system instructions a kernel entry path needs.

Two levels of check:

  1. **Instruction level** -- single instructions assembled in a given mode by
     both rasm and GNU `as`, compared byte for byte. 32-bit mode is where the
     encodings diverge most from what ShivyCX emits: no REX, one-byte inc/dec,
     the `moffs` accumulator moves, and absolute addressing without a SIB byte.
  2. **File level** -- the real Multiboot entry file from the mbos bare-metal
     kernel (`examples/rpython2c/mbos/boot64.S`), which starts in 32-bit
     protected mode, builds page tables, enables long mode and far-jumps into
     64-bit code. Every section is compared against `as`.

The file-level check is the one that matters: it exercises `.set` expressions,
symbol-difference data (`gdt64_ptr - gdt64 - 1`), numeric local labels
(`1:` / `jne 1b`), C block comments, labels sharing a line with a directive,
and both code modes in one translation unit.
"""
import os
import sys
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rasm
import rasm_obj

REPO = os.path.dirname(os.path.dirname(HERE))
BOOT64 = os.path.join(REPO, "examples", "rpython2c", "mbos", "boot64.S")

# (mode, instruction) pairs
CASES = [
    # --- 32-bit: no REX, short forms ---
    (32, "mov $0x1000, %esp"),
    (32, "mov %eax, %ebx"),
    (32, "mov %eax, 0x400000"),
    (32, "mov 0x400000, %eax"),
    (32, "mov %ebx, 0x400000"),
    (32, "mov %eax, (%edi)"),
    (32, "mov %eax, 4(%edi)"),
    (32, "mov %ecx, (%edi,%ebx,4)"),
    (32, "inc %ecx"),
    (32, "dec %edx"),
    (32, "inc %eax"),
    (32, "add $8, %edi"),
    (32, "or $0x83, %eax"),
    (32, "or $0x80000001, %eax"),
    (32, "and $0xFFFFFFFB, %eax"),
    (32, "xor %eax, %eax"),
    (32, "shl $21, %eax"),
    (32, "shr $11, %eax"),
    (32, "cmp $2048, %ecx"),
    (32, "test %eax, %eax"),
    (32, "push %ebp"),
    (32, "pop %ebx"),
    (32, "cli"),
    (32, "cld"),
    (32, "hlt"),
    (32, "rep stosl"),
    (32, "rep movsl"),
    # --- 32-bit: system / privileged ---
    (32, "mov %cr0, %eax"),
    (32, "mov %eax, %cr0"),
    (32, "mov %cr3, %eax"),
    (32, "mov %eax, %cr3"),
    (32, "mov %cr4, %eax"),
    (32, "mov %eax, %cr4"),
    (32, "mov %eax, %ds"),
    (32, "mov %ds, %eax"),
    (32, "rdmsr"),
    (32, "wrmsr"),
    (32, "clts"),
    (32, "invd"),
    (32, "wbinvd"),
    (32, "iret"),
    # --- 64-bit: the same system instructions ---
    (64, "mov %cr0, %rax"),
    (64, "mov %rax, %cr3"),
    (64, "mov %cr4, %rax"),
    (64, "mov %ax, %ds"),
    (64, "mov %ax, %ss"),
    (64, "mov %ax, %es"),
    (64, "mov %ax, %fs"),
    (64, "mov %ax, %gs"),
    (64, "rdmsr"),
    (64, "wrmsr"),
    (64, "hlt"),
    (64, "cli"),
    (64, "iretq"),
    (64, "xor %rbp, %rbp"),
    (64, "movq $0x1000, %rsp"),
    # 32-bit address size in 64-bit mode needs the 0x67 prefix
    (64, "mov %eax, (%edi)"),
    (64, "mov (%esi), %ecx"),
]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def gas_bytes(mode, line, work):
    src = os.path.join(work, "t.s")
    obj = os.path.join(work, "t.o")
    binf = os.path.join(work, "t.bin")
    with open(src, "w") as f:
        f.write(".code%d\n.text\n%s\n" % (mode, line))
    if run(["as", "--64", "-o", obj, src]).returncode != 0:
        return None
    run(["objcopy", "-O", "binary", "--only-section=.text", obj, binf])
    with open(binf, "rb") as f:
        return list(f.read())


def rasm_bytes(mode, line):
    rasm.set_mode(mode)
    try:
        kind, mnem, ops = rasm.parse_att_line(line)
        if kind != "insn":
            return None
        body, relocs = rasm.encode(mnem, ops)
        return body
    finally:
        rasm.set_mode(64)


def hexs(bs):
    return " ".join("%02x" % b for b in bs)


def section_bytes(obj, name, work):
    binf = os.path.join(work, "s.bin")
    if os.path.exists(binf):
        os.remove(binf)
    run(["objcopy", "-O", "binary", "--only-section=" + name, obj, binf])
    if not os.path.exists(binf):
        return b""
    with open(binf, "rb") as f:
        return f.read()


def main():
    work = tempfile.mkdtemp(prefix="rasm_bare_")
    passed = failed = 0

    print("== instruction level: .code32 / .code64 vs GNU as ==")
    for mode, line in CASES:
        want = gas_bytes(mode, line, work)
        if want is None:
            print("  SKIP (as rejected) [%d] %s" % (mode, line))
            continue
        try:
            got = rasm_bytes(mode, line)
        except Exception as e:
            print("  FAIL [%d] %-28s rasm error: %s" % (mode, line, e))
            failed += 1
            continue
        if got == want:
            passed += 1
        else:
            failed += 1
            print("  FAIL [%d] %-28s rasm=%s  as=%s"
                  % (mode, line, hexs(got), hexs(want)))

    print("\n== file level: the mbos Multiboot entry (boot64.S) ==")
    if not os.path.exists(BOOT64):
        print("  SKIP  boot64.S not present")
    else:
        obj_as = os.path.join(work, "boot_as.o")
        obj_r = os.path.join(work, "boot_rasm.o")
        r = run(["as", "--64", "-o", obj_as, BOOT64])
        if r.returncode != 0:
            print("  SKIP  as could not assemble boot64.S")
        else:
            try:
                with open(BOOT64) as f:
                    elf = rasm_obj.assemble_to_elf(f.read())
                with open(obj_r, "wb") as f:
                    f.write(bytes(bytearray(elf)))
                ok = True
                for sec in [".text", ".rodata"]:
                    a = section_bytes(obj_as, sec, work)
                    b = section_bytes(obj_r, sec, work)
                    if a == b and len(a) > 0:
                        print("  ok    %-10s %d bytes, byte-identical" %
                              (sec, len(a)))
                    else:
                        print("  FAIL  %-10s as=%d rasm=%d bytes"
                              % (sec, len(a), len(b)))
                        ok = False
                if ok:
                    passed += 1
                else:
                    failed += 1
            except Exception as e:
                print("  FAIL  rasm could not assemble boot64.S: %s" % e)
                failed += 1

    total = passed + failed
    print("\nrasm bare-metal differential: %d/%d passed" % (passed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
