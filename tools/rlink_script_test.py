#!/usr/bin/env python3
"""Differential test: rlink's linker-script layout vs GNU `ld`.

A linker script is what defines a bare-metal image -- its load address, which
section leads, how sections are aligned, and the symbols (`__bss_start`,
`__stack_top`, a page-table region) that the boot code resolves against. Get
any of it wrong and the failure is not a link error: the image links, boots,
and misbehaves somewhere else entirely. A vector table 1 KiB out of alignment
is accepted silently by VBAR_EL1, which just ignores the low bits.

So each script here is linked twice -- once by `ld`, once by rlink -- and the
two are compared on what actually matters:

  * every script-defined symbol resolves to the same address
  * the entry point matches
  * alignment constraints the script asked for are honoured
  * for the full bare-metal image, both binaries boot under qemu and produce
    identical console output

Using `ld` as the oracle is the point. Checking rlink against its own idea of
what the script means would pass no matter what the script means.

    python3 tools/rlink_script_test.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RLINK = os.path.join(HERE, "rpy_lib", "rlink.py")
BAREMETAL = os.path.join(ROOT, "baremetal64")

LD = "aarch64-linux-gnu-ld"
NM = "aarch64-linux-gnu-nm"
READELF = "aarch64-linux-gnu-readelf"

sys.path.insert(0, os.path.join(HERE, "rpy_lib"))


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return p.returncode, p.stdout, p.stderr


def have(tool):
    return subprocess.run(["which", tool], capture_output=True).returncode == 0


def assemble(text, path):
    import rasm_obj
    data = rasm_obj.assemble_to_elf(text, "arm64")
    with open(path, "wb") as f:
        f.write(bytes(data))
    return path


def symbols(elf):
    rc, out, _ = run([NM, elf])
    syms = {}
    if rc != 0:
        return syms
    for line in out.split("\n"):
        parts = line.split()
        if len(parts) >= 3:
            try:
                syms[parts[2]] = int(parts[0], 16)
            except ValueError:
                pass
    return syms


def entry_of(elf):
    rc, out, _ = run([READELF, "-h", elf])
    for line in out.split("\n"):
        if "Entry point" in line:
            return int(line.split()[-1], 16)
    return -1


# ---------------------------------------------------------------------------
# Synthetic cases: one small object, several scripts exercising one feature
# each. The object deliberately has .text, .rodata, .data and .bss so every
# script has something to place.
# ---------------------------------------------------------------------------
OBJ_SRC = """
.section .text
.global _start
_start:
    mov     x0, #1
    ret

.section .rodata
.balign 8
rodata_item:
    .quad 0x1122334455667788

.section .data
.balign 8
data_item:
    .quad 0x99aabbccddeeff00

.section .bss
.balign 8
bss_item:
    .skip 64
"""

SCRIPTS = [
    ("plain load address", """
ENTRY(_start)
SECTIONS {
    . = 0x40080000;
    .text : { *(.text) }
    .rodata : { *(.rodata) }
    .data : { *(.data) }
    .bss : { *(.bss) *(COMMON) }
}
"""),
    ("section ALIGN attribute", """
ENTRY(_start)
SECTIONS {
    . = 0x40080000;
    .text : { *(.text) }
    .rodata ALIGN(2048) : { *(.rodata) }
    .data ALIGN(4096) : { *(.data) }
    .bss : { *(.bss) *(COMMON) }
}
"""),
    ("dot ALIGN between sections", """
ENTRY(_start)
SECTIONS {
    . = 0x40080000;
    .text : { *(.text) }
    . = ALIGN(4096);
    marker_a = .;
    .rodata : { *(.rodata) }
    . = ALIGN(256);
    marker_b = .;
    .data : { *(.data) }
    .bss : { *(.bss) *(COMMON) }
}
"""),
    ("reserved regions via . = . + n", """
ENTRY(_start)
SECTIONS {
    . = 0x40080000;
    .text : { *(.text) }
    .rodata : { *(.rodata) }
    .data : { *(.data) }
    .bss : { *(.bss) *(COMMON) }
    . = ALIGN(4096);
    region_start = .;
    . = . + 0x4000;
    region_end = .;
    . = ALIGN(16);
    stack_bottom = .;
    . = . + 0x10000;
    stack_top = .;
}
"""),
    ("symbols bracketing bss", """
ENTRY(_start)
SECTIONS {
    . = 0x40080000;
    .text : { *(.text) }
    .rodata : { *(.rodata) }
    .data : { *(.data) }
    . = ALIGN(8);
    __bss_start = .;
    .bss : { *(.bss) *(COMMON) . = ALIGN(8); }
    __bss_end = .;
    __image_end = .;
}
"""),
    ("end-of-section align inside .bss", """
ENTRY(_start)
SECTIONS {
    . = 0x40080000;
    .text : { *(.text) }
    .rodata : { *(.rodata) }
    .data : { *(.data) }
    __bss_start = .;
    .bss : { *(.bss) *(COMMON) . = ALIGN(256); }
    __bss_end = .;
    after_bss = .;
}
"""),
    ("KEEP and a leading section", """
ENTRY(_start)
SECTIONS {
    . = 0x40080000;
    .text : { KEEP(*(.text)) *(.text.*) }
    .rodata : { *(.rodata) }
    .data : { *(.data) }
    .bss : { *(.bss) *(COMMON) }
    /DISCARD/ : { *(.comment) *(.note.*) }
}
"""),
    ("arithmetic in an expression", """
ENTRY(_start)
SECTIONS {
    . = 0x40000000 + 0x80000;
    .text : { *(.text) }
    .rodata : { *(.rodata) }
    .data : { *(.data) }
    .bss : { *(.bss) *(COMMON) }
    . = ALIGN(4096);
    computed = . + 16 * 4;
}
"""),
]

# Symbols that only exist because a script defined them. Comparing these is
# the sharpest check: they are pure layout, with no input section to anchor
# them, so any disagreement about where `.` had got to shows up here.
INTERESTING = [
    "marker_a", "marker_b", "region_start", "region_end",
    "stack_bottom", "stack_top", "__bss_start", "__bss_end",
    "__image_end", "computed", "_start", "after_bss",
]


def section_addrs(elf):
    """Allocated section name -> address, from `readelf -S`.

    readelf prints the index as `[ 2]`, which whitespace-splits into `[` and
    `2]` -- so the name is not at a fixed column. Splitting on `]` first is
    what makes this robust; getting it wrong makes the check silently match
    nothing, which is exactly how the first version of this test passed while
    measuring nothing at all.
    """
    rc, out, _ = run([READELF, "-S", "-W", elf])
    addrs = {}
    if rc != 0:
        return addrs
    for line in out.split("\n"):
        if "]" not in line or "[" not in line:
            continue
        rest = line.split("]", 1)[1].split()
        if len(rest) < 3:
            continue
        name, stype, addr = rest[0], rest[1], rest[2]
        if not name.startswith("."):
            continue
        if stype in ("SYMTAB", "STRTAB", "NULL"):
            continue
        try:
            addrs[name] = int(addr, 16)
        except ValueError:
            continue
    return addrs


def check_alignments(name, syms, elf):
    """Alignment requirements a script stated, checked on the result."""
    problems = []
    if "ALIGN attribute" in name:
        addrs = section_addrs(elf)
        for sec, align in ((".rodata", 2048), (".data", 4096)):
            if sec not in addrs:
                problems.append("%s absent from the image" % sec)
            elif addrs[sec] % align != 0:
                problems.append("%s at 0x%x is not %d-aligned"
                                % (sec, addrs[sec], align))
    for nm, align in (("after_bss", 256),
                      ("region_start", 4096), ("stack_bottom", 16),
                      ("marker_a", 4096), ("marker_b", 256)):
        if nm in syms and syms[nm] % align != 0:
            problems.append("%s at 0x%x is not %d-aligned"
                            % (nm, syms[nm], align))
    return problems


def compare_sections(gnu, ours):
    """Every allocated section must sit at the same address in both images.

    This is the check with the widest reach: a script feature that rlink
    ignores usually shows up as a section in the wrong place long before it
    shows up as a wrong symbol, and unlike the symbol comparison it needs no
    script to have named anything.
    """
    ga, oa = section_addrs(gnu), section_addrs(ours)
    problems = []
    for name in sorted(ga.keys()):
        if ga[name] == 0:
            continue          # not allocated
        if name not in oa:
            problems.append("%s missing from rlink output" % name)
        elif ga[name] != oa[name]:
            problems.append("%s: rlink 0x%x, ld 0x%x"
                            % (name, oa[name], ga[name]))
    return problems


def run_synthetic():
    npass = nfail = 0
    with tempfile.TemporaryDirectory() as d:
        obj = assemble(OBJ_SRC, os.path.join(d, "t.o"))
        for name, script in SCRIPTS:
            spath = os.path.join(d, "t.ld")
            with open(spath, "w") as f:
                f.write(script)
            gnu = os.path.join(d, "gnu.elf")
            ours = os.path.join(d, "ours.elf")

            rc, _, err = run([LD, "-T", spath, "-o", gnu, obj])
            if rc != 0:
                print("  SKIP  %-32s GNU ld rejected the script: %s"
                      % (name, err.strip().split("\n")[0]))
                continue
            rc, out, err = run([sys.executable, RLINK, "-T", spath,
                                "-o", ours, obj])
            if rc != 0 or not os.path.exists(ours):
                print("  FAIL  %-32s rlink failed: %s"
                      % (name, (out + err).strip().split("\n")[0]))
                nfail += 1
                continue

            gs, os_ = symbols(gnu), symbols(ours)
            problems = []
            if entry_of(gnu) != entry_of(ours):
                problems.append("entry 0x%x vs 0x%x"
                                % (entry_of(ours), entry_of(gnu)))
            for sym in INTERESTING:
                if sym in gs:
                    if sym not in os_:
                        problems.append("%s missing from rlink output" % sym)
                    elif gs[sym] != os_[sym]:
                        problems.append("%s: rlink 0x%x, ld 0x%x"
                                        % (sym, os_[sym], gs[sym]))
            problems.extend(check_alignments(name, os_, ours))
            problems.extend(compare_sections(gnu, ours))

            if problems:
                print("  FAIL  %-32s %s" % (name, problems[0]))
                for p in problems[1:]:
                    print("        %-32s %s" % ("", p))
                nfail += 1
            else:
                print("  PASS  %s" % name)
                npass += 1
    return npass, nfail


# ---------------------------------------------------------------------------
# The real thing: link the bare-metal kernel both ways and boot both.
# ---------------------------------------------------------------------------
def run_image():
    sys.path.insert(0, HERE)
    import baremetal_arm64 as bm

    if not have("qemu-system-aarch64"):
        print("  SKIP  full image: qemu-system-aarch64 not installed")
        return 0, 0

    app = os.path.join(ROOT, "examples", "baremetal", "kernel_arm64.c")
    npass = nfail = 0
    with tempfile.TemporaryDirectory() as d:
        objdir = os.path.join(d, "obj")
        gnu = os.path.join(d, "gnu.elf")
        ours = os.path.join(d, "ours.elf")
        try:
            bm.build([app], gnu, objdir, gnu_ld=True)
            bm.build([app], ours, objdir, gnu_ld=False)
        except Exception as e:
            print("  FAIL  full image: build failed: %s" % e)
            return 0, 1

        gs, os_ = symbols(gnu), symbols(ours)
        problems = []
        for sym in ("_start", "__bss_start", "__bss_end", "__pgtbl_start",
                    "__pgtbl_end", "__stack_top", "vectors_arm64"):
            if sym in gs and sym in os_ and gs[sym] != os_[sym]:
                problems.append("%s: rlink 0x%x, ld 0x%x"
                                % (sym, os_[sym], gs[sym]))
        # VBAR_EL1 ignores the low 11 bits, so this one must hold exactly.
        if "vectors_arm64" in os_ and os_["vectors_arm64"] % 2048 != 0:
            problems.append("vectors_arm64 at 0x%x is not 2048-aligned"
                            % os_["vectors_arm64"])
        problems.extend(compare_sections(gnu, ours))
        if problems:
            for p in problems:
                print("  FAIL  full image: %s" % p)
            nfail += 1
        else:
            print("  PASS  full image: symbol addresses agree with ld")
            npass += 1

        out_gnu = bm.qemu_run(gnu)
        out_ours = bm.qemu_run(ours)
        marker = "all stages ok"
        if marker not in out_ours:
            print("  FAIL  full image: rlink-linked image did not complete")
            nfail += 1
        elif _normalise(out_gnu) != _normalise(out_ours):
            print("  FAIL  full image: console output differs from the "
                  "ld-linked build")
            nfail += 1
        else:
            print("  PASS  full image: boots and matches ld's build exactly")
            npass += 1

        gsize = os.path.getsize(gnu)
        osize = os.path.getsize(ours)
        print("        (ld %d bytes, rlink %d bytes)" % (gsize, osize))
    return npass, nfail


def _normalise(text):
    """Drop addresses that legitimately differ between the two builds.

    The two linkers lay out .text identically but not the symbol table or
    section headers, and ELR values printed by the exception handler are
    absolute code addresses -- so compare the console output with hex numbers
    masked out.
    """
    out = []
    for line in text.replace("\r", "").split("\n"):
        if "ELR" in line or "TTBR0" in line:
            continue
        out.append(line.rstrip())
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# x86-64 multiboot: the pre-existing user of scripts, and the case that showed
# script-named sections must be placed regardless of SHF_ALLOC.
# ---------------------------------------------------------------------------
def run_multiboot():
    """The real boot64.S against the real kernel64.ld, both linkers.

    `.section .multiboot` in gas carries no flags unless the source spells
    them out, and boot64.S does not. rlink used to require SHF_ALLOC to place
    an input section, so the 12-byte Multiboot header was silently dropped --
    `_start` ended up at the image's first byte and GRUB would have refused
    the image for having no header in its first 8 KiB. Nothing about that
    failure looks like a linker bug from the outside.
    """
    if not have("as") or not have("ld"):
        print("  SKIP  multiboot: host binutils not installed")
        return 0, 0
    src = os.path.join(BAREMETAL, "boot64.S")
    script = os.path.join(BAREMETAL, "kernel64.ld")
    if not (os.path.exists(src) and os.path.exists(script)):
        print("  SKIP  multiboot: boot64.S / kernel64.ld not present")
        return 0, 0

    npass = nfail = 0
    with tempfile.TemporaryDirectory() as d:
        obj = os.path.join(d, "boot64.o")
        rc, _, err = run(["as", "--64", "-o", obj, src])
        if rc != 0:
            print("  SKIP  multiboot: assembling boot64.S failed")
            return 0, 0
        # boot64.S calls kmain(); supply a stub so the link resolves.
        stub_s = os.path.join(d, "stub.s")
        with open(stub_s, "w") as f:
            f.write(".text\n.global kmain\nkmain:\n\tret\n")
        stub = os.path.join(d, "stub.o")
        run(["as", "--64", "-o", stub, stub_s])

        gnu = os.path.join(d, "gnu.elf")
        ours = os.path.join(d, "ours.elf")
        rc, _, err = run(["ld", "-T", script, "-o", gnu, obj, stub])
        if rc != 0:
            print("  SKIP  multiboot: GNU ld rejected the link: %s"
                  % err.strip().split("\n")[0])
            return 0, 0
        rc, out, err = run([sys.executable, RLINK, "-T", script, "-o", ours,
                            obj, stub])
        if rc != 0 or not os.path.exists(ours):
            print("  FAIL  multiboot: rlink failed: %s"
                  % (out + err).strip().split("\n")[0])
            return 0, 1

        problems = []
        if entry_of(gnu) != entry_of(ours):
            problems.append("entry 0x%x vs ld's 0x%x"
                            % (entry_of(ours), entry_of(gnu)))
        # The header must be the first thing in the loaded image.
        magic = _load_head(ours, 4)
        if magic != _load_head(gnu, 4):
            problems.append("image does not start with the same bytes as ld's")
        elif magic != [0x02, 0xB0, 0xAD, 0x1B]:
            problems.append("image does not start with the Multiboot magic")
        if problems:
            for p in problems:
                print("  FAIL  multiboot: %s" % p)
            nfail += 1
        else:
            print("  PASS  multiboot: header leads the image, entry matches ld")
            npass += 1
    return npass, nfail


def _load_head(elf, n):
    """First `n` bytes of the first PT_LOAD segment."""
    rc, out, _ = run([READELF, "-lW", elf])
    off = -1
    for line in out.split("\n"):
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "LOAD":
            off = int(parts[1], 16)
            break
    if off < 0:
        return []
    with open(elf, "rb") as f:
        f.seek(off)
        return list(f.read(n))


# ---------------------------------------------------------------------------
# Constructs rlink does not implement must be *rejected*, not skipped.
# ---------------------------------------------------------------------------
# Every one of these moves things, so quietly ignoring any of them yields a
# layout that differs from what the script asked for. A bare-metal image whose
# sections are somewhere else fails at boot with nothing pointing back at the
# script, which is far worse than a link that refuses to start.
REJECT_CASES = [
    ("MEMORY regions", """
MEMORY { ram (rwx) : ORIGIN = 0x40000000, LENGTH = 64K }
SECTIONS { .text : { *(.text) } }
"""),
    ("AT() load address", """
SECTIONS { .text : AT(0x2000) { *(.text) } }
"""),
    ("SORT() in a section body", """
SECTIONS { .text : { SORT(*(.text.*)) } }
"""),
    ("OVERLAY", """
SECTIONS { OVERLAY 0x1000 : { .a { *(.a) } } }
"""),
    ("MAX() in an expression", """
SECTIONS { . = MAX(0x1000, 0x2000); .text : { *(.text) } }
"""),
    ("PHDRS", """
PHDRS { h PT_LOAD; }
SECTIONS { .text : { *(.text) } }
"""),
]


def run_rejections():
    import rlink
    npass = nfail = 0
    for name, script in REJECT_CASES:
        try:
            sc = rlink.parse_script(script)
            # Expressions are evaluated at layout time, so a rejection may
            # only surface then; force it here.
            for cmd in sc.cmds:
                if cmd.kind == 0:
                    rlink._eval_expr(cmd.expr, 0, {})
            print("  FAIL  %-28s accepted silently" % name)
            nfail += 1
        except rlink.LinkError:
            print("  PASS  %-28s rejected" % name)
            npass += 1
        except Exception as e:
            print("  FAIL  %-28s raised %s, not LinkError"
                  % (name, type(e).__name__))
            nfail += 1
    return npass, nfail


# ---------------------------------------------------------------------------
# Relocations that splice an immediate into an instruction word.
# ---------------------------------------------------------------------------
# `adr` was added to rasm with an `adr_prel_lo21` relocation kind, but nothing
# taught rasm_arch or rlink what that kind meant. It fell through to the *data*
# path and became a PREL32 -- a plain 32-bit write that overwrote the whole
# instruction word instead of splicing the immediate into it, turning every
# `adr` into a `udf`.
#
# It stayed hidden for three sessions because the only `adr` in the tree is in
# the boot stub's exception-level descent, and `qemu -M virt` enters at EL1 --
# so that code never ran. It surfaced the moment a Raspberry Pi, which enters
# at EL3, executed it.
#
# The lesson generalises: checking that an encoder emits the right *unrelocated*
# word and attaches a relocation of the right name says nothing about whether
# anything downstream knows how to apply it. So this compares the fully linked
# bytes against GNU's.
RELOC_ASM = """
.text
.global _start
_start:
    adr     x0, target
    adr     x30, target
    adrp    x1, target
    add     x1, x1, :lo12:target
    b       target
    bl      target
    nop
target:
    ret
"""


def run_relocs():
    if not have("aarch64-linux-gnu-as"):
        print("  SKIP  relocations: cross assembler not installed")
        return 0, 0
    import rasm_obj

    npass = nfail = 0
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "r.s")
        with open(src, "w") as f:
            f.write(RELOC_ASM)

        gnu_o = os.path.join(d, "gnu.o")
        our_o = os.path.join(d, "our.o")
        rc, _, err = run(["aarch64-linux-gnu-as", "-o", gnu_o, src])
        if rc != 0:
            print("  SKIP  relocations: GNU as rejected the source")
            return 0, 0
        with open(our_o, "wb") as f:
            f.write(bytes(rasm_obj.assemble_to_elf(RELOC_ASM, "arm64")))

        # Link both with the same linker so only the *object* differs, then
        # link ours with rlink too so the applier is exercised as well.
        outs = {}
        for tag, obj in (("gnu", gnu_o), ("ours", our_o)):
            elf = os.path.join(d, tag + ".elf")
            rc, _, _ = run(["aarch64-linux-gnu-ld", "-Ttext=0x1000",
                            "-o", elf, obj])
            if rc != 0:
                print("  FAIL  relocations: linking %s failed" % tag)
                return 0, 1
            outs[tag] = disasm_text(elf)

        if outs["gnu"] != outs["ours"]:
            print("  FAIL  relocations: rasm's object links to different code "
                  "than GNU as's")
            for a, b in zip(outs["gnu"].split("\n"), outs["ours"].split("\n")):
                if a != b:
                    print("        gnu : %s" % a)
                    print("        ours: %s" % b)
                    break
            nfail += 1
        else:
            print("  PASS  relocations: rasm object links identically to GNU's")
            npass += 1

        # And the same object through rlink.
        elf = os.path.join(d, "rlink.elf")
        script = os.path.join(d, "r.ld")
        with open(script, "w") as f:
            f.write("ENTRY(_start)\nSECTIONS { . = 0x1000;"
                    " .text : { *(.text) } }\n")
        rc, out, err = run([sys.executable, RLINK, "-T", script,
                            "-o", elf, our_o])
        if rc != 0:
            print("  FAIL  relocations: rlink failed: %s"
                  % (out + err).strip().split("\n")[0])
            nfail += 1
        elif disasm_text(elf) != outs["gnu"]:
            print("  FAIL  relocations: rlink applied them differently to ld")
            for a, b in zip(outs["gnu"].split("\n"),
                            disasm_text(elf).split("\n")):
                if a != b:
                    print("        ld   : %s" % a)
                    print("        rlink: %s" % b)
                    break
            nfail += 1
        else:
            print("  PASS  relocations: rlink applies them exactly as ld does")
            npass += 1
    return npass, nfail


def disasm_text(elf):
    """Disassembled .text, normalised to mnemonics and operands only."""
    rc, out, _ = run(["aarch64-linux-gnu-objdump", "-d", "--section=.text",
                      elf])
    lines = []
    for line in out.split("\n"):
        if ":\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        lines.append(parts[2].strip().split("//")[0].strip())
    return "\n".join(lines)


def main():
    for tool in (LD, NM, READELF):
        if not have(tool):
            print("SKIP: %s not installed" % tool)
            return 0

    print("== script features, differentially against ld ==")
    p1, f1 = run_synthetic()
    print("\n== the bare-metal image, linked both ways ==")
    p2, f2 = run_image()
    print("\n== x86-64 multiboot, the other script user ==")
    p3, f3 = run_multiboot()
    print("\n== unsupported constructs are refused, not ignored ==")
    p4, f4 = run_rejections()
    print("\n== instruction-splicing relocations ==")
    p5, f5 = run_relocs()

    npass, nfail = p1 + p2 + p3 + p4 + p5, f1 + f2 + f3 + f4 + f5
    print("\nrlink linker scripts: %d pass, %d fail" % (npass, nfail))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
