#!/usr/bin/env python3
"""Embed a CRUSTOS PT_NOTE with reg_class into an ELF (Crust-ELF hint).

    python3 tools/crust_elf_note.py guest.so --reg-class 1 -o guest_hinted.so

If -o is omitted, rewrites in place via a temp file. reg_class: 0..3
(minimal / extra / full GPR / +xmm).
"""
from __future__ import print_function

import argparse
import os
import struct
import sys
import tempfile

# ELF64 little-endian
EI_NIDENT = 16
PT_NOTE = 4
PT_NULL = 0


def _u16(b, o): return struct.unpack_from("<H", b, o)[0]
def _u32(b, o): return struct.unpack_from("<I", b, o)[0]
def _u64(b, o): return struct.unpack_from("<Q", b, o)[0]
def _su16(b, o, v): struct.pack_into("<H", b, o, v)
def _su32(b, o, v): struct.pack_into("<I", b, o, v)
def _su64(b, o, v): struct.pack_into("<Q", b, o, v)


def add_crustos_note(data, reg_class):
    data = bytearray(data)
    if data[0:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ValueError("need ELF64 LE")
    e_type = _u16(data, 16)
    e_phoff = _u64(data, 32)
    e_flags = _u32(data, 48)
    e_phentsize = _u16(data, 54)
    e_phnum = _u16(data, 56)

    # Also encode in e_flags bits 8..9 for loaders that skip notes.
    e_flags = (e_flags & ~0x300) | ((reg_class & 3) << 8)
    _su32(data, 48, e_flags)

    # Build note payload: namesz=8 ("CRUSTOS\\0"), descsz=1, type=1, name, desc
    name = b"CRUSTOS\0"
    namesz = len(name)
    descsz = 1
    ntype = 1
    note = struct.pack("<III", namesz, descsz, ntype) + name
    while len(note) % 4:
        note += b"\0"
    note += bytes([reg_class & 0xff])
    while len(note) % 4:
        note += b"\0"

    # Append note bytes; add a PT_NOTE phdr. Grow file: append note, rewrite
    # program headers into a new table at end that includes the old ones + note.
    note_off = len(data)
    data.extend(note)

    old_ph = bytes(data[e_phoff:e_phoff + e_phnum * e_phentsize])
    new_phoff = len(data)
    # pad to 8
    while len(data) % 8:
        data.append(0)
    new_phoff = len(data)

    data.extend(old_ph)
    # New phdr: PT_NOTE
    phdr = bytearray(e_phentsize)
    _su32(phdr, 0, PT_NOTE)
    _su32(phdr, 4, 4)  # PF_R
    _su64(phdr, 8, note_off)
    _su64(phdr, 16, 0)  # vaddr
    _su64(phdr, 24, 0)
    _su64(phdr, 32, len(note))
    _su64(phdr, 40, len(note))
    _su64(phdr, 48, 4)
    data.extend(phdr)

    _su64(data, 32, new_phoff)  # e_phoff
    _su16(data, 56, e_phnum + 1)  # e_phnum
    return bytes(data)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("elf")
    ap.add_argument("--reg-class", type=int, default=1, choices=(0, 1, 2, 3))
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args(argv)
    with open(args.elf, "rb") as f:
        raw = f.read()
    out = add_crustos_note(raw, args.reg_class)
    dest = args.output or args.elf
    if dest == args.elf:
        fd, tmp = tempfile.mkstemp(suffix=".elf")
        os.close(fd)
        with open(tmp, "wb") as f:
            f.write(out)
        os.replace(tmp, dest)
    else:
        with open(dest, "wb") as f:
            f.write(out)
    print("wrote %s with CRUSTOS note reg_class=%d" % (dest, args.reg_class))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
