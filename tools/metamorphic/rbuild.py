#!/usr/bin/env python3
"""Assemble a hand-written .s with rasm and link it with rlink at a chosen
base address, pulling in the freestanding rcrt.s + rlibc.o runtime. This is
the same path shivyc/main.py uses for SHIVYC_RLINK, exposed directly so we
can feed our own assembly and pick the load base."""
import os, sys, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RLIB = os.path.join(ROOT, "tools", "rpy_lib")
sys.path.insert(0, RLIB)
import rasm_obj, rlink


def build(asm_path, out_path, base=0x1000):
    ln = rlink.Linker()
    ln.entry_name = "_start"
    ln.base = base

    # freestanding runtime: rcrt.s (assembled now) + rlibc.o (cached by main.py)
    crt_s = os.path.join(RLIB, "rcrt.s")
    with open(crt_s) as f:
        ln.add_object(crt_s, list(rasm_obj.assemble_to_elf(f.read())))
    rlibc_o = os.path.join(RLIB, "build", "rlibc.o")
    if not os.path.exists(rlibc_o):
        raise SystemExit("rlibc.o not built; run a SHIVYC_RLINK build once first")
    with open(rlibc_o, "rb") as f:
        ln.add_object(rlibc_o, list(f.read()))

    # our benchmark object
    with open(asm_path) as f:
        ln.add_object(asm_path, list(rasm_obj.assemble_to_elf(f.read())))

    image = ln.link()
    with open(out_path, "wb") as f:
        f.write(bytes(bytearray(image)))
    os.chmod(out_path, 0o755)
    for w in ln.warnings:
        sys.stderr.write("rlink: warning: %s\n" % w)


if __name__ == "__main__":
    base = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x1000
    build(sys.argv[1], sys.argv[2], base)
    print("built %s (base 0x%x)" % (sys.argv[2], base))
