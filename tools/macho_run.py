#!/usr/bin/env python3
"""Run a self-contained arm64 Mach-O executable's main() under emulation.

There is no Darwin kernel off a Mac, but code that makes no system calls --
pure computation, including calls across compiler boundaries -- needs none.
This loads a linked MH_EXECUTE's segments at their link addresses into the
Unicorn CPU emulator, gives it a stack, and calls the LC_MAIN entry point
with a sentinel return address; main's return value is the result.

That makes Apple's arm64 calling convention *executable* on Linux: link
Crust-compiled code with clang-compiled code (`clang -target
arm64-apple-macos11`, the reference implementation of the ABI) and run it.

Requirements on the image, checked here:
* No dylib imports (nothing to bind; dyld is not emulated).
* Linked at its preferred address with classic fixups (`ld64.lld
  -no_fixup_chains`), so no rebasing is needed: pointers in data already
  hold their final values. Chained fixups would leave encoded chains there.

    python3 tools/macho_run.py prog      # exit status = main's return & 0xff

Needs `pip install unicorn`.
"""
import struct
import sys

MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_ARM64 = 0x0100000C
LC_SEGMENT_64 = 0x19
LC_MAIN = 0x80000028
LC_DYLD_CHAINED_FIXUPS = 0x80000034
LC_DYLD_INFO = 0x22
LC_DYLD_INFO_ONLY = 0x80000022

SENTINEL = 0x10000            # main "returns" here; emulation stops
STACK_TOP = 0x7F0000000
STACK_SIZE = 8 << 20


class MachOError(Exception):
    pass


def _page(n, size=0x1000):
    return (n + size - 1) & ~(size - 1)


def load(path):
    """Parse `path`: returns (segments, entry_vmaddr). Each segment is
    (name, vmaddr, vmsize, bytes)."""
    with open(path, "rb") as f:
        data = f.read()
    magic, cpu, _sub, ftype, ncmds, _size, _flags, _res = \
        struct.unpack_from("<IiiIIIII", data, 0)
    if magic != MH_MAGIC_64 or cpu != CPU_TYPE_ARM64:
        raise MachOError("not an arm64 Mach-O")
    if ftype != 2:
        raise MachOError("not an executable (MH_EXECUTE)")
    off = 32
    segs = []
    entryoff = None
    text_vm = None
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, off)
        if cmd == LC_SEGMENT_64:
            name = data[off + 8:off + 24].rstrip(b"\0").decode()
            vmaddr, vmsize, fileoff, filesize = \
                struct.unpack_from("<QQQQ", data, off + 24)
            if name != "__PAGEZERO":
                segs.append((name, vmaddr, vmsize,
                             data[fileoff:fileoff + filesize]))
            if name == "__TEXT":
                text_vm = vmaddr
        elif cmd == LC_MAIN:
            entryoff, = struct.unpack_from("<Q", data, off + 8)
        elif cmd == LC_DYLD_CHAINED_FIXUPS:
            raise MachOError("linked with chained fixups; relink with "
                             "-no_fixup_chains")
        elif cmd in (LC_DYLD_INFO, LC_DYLD_INFO_ONLY):
            # dyld_info_command: cmd, cmdsize, then (off, size) pairs for
            # rebase, bind, weak_bind, lazy_bind, export.
            bind_off, bind_size = struct.unpack_from("<II", data, off + 16)
            lazy_off, lazy_size = struct.unpack_from("<II", data, off + 32)
            if _has_binds(data, bind_off, bind_size) or \
                    _has_binds(data, lazy_off, lazy_size):
                raise MachOError("image imports symbols from a dylib; "
                                 "only self-contained code can run here")
        off += cmdsize
    if entryoff is None or text_vm is None:
        raise MachOError("no LC_MAIN entry point")
    return segs, text_vm + entryoff


def _uleb(data, i):
    """Decode a ULEB128 at data[i]; returns (value, next index)."""
    v = shift = 0
    while True:
        b = data[i]
        i += 1
        v |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return v, i


def _has_binds(data, off, size):
    """True if a bind-opcode stream binds any symbol other than
    dyld_stub_binder (which ld64 always names). Decodes each opcode with its
    operands: a byte-level scan would misread ULEB128 operands as opcodes.
    DONE is treated as a separator, as it is in lazy-bind streams."""
    i, end = off, off + size
    sym = None
    while i < end:
        op, imm = data[i] & 0xF0, data[i] & 0x0F
        i += 1
        if op == 0x40:                       # SET_SYMBOL_TRAILING_FLAGS_IMM
            j = data.index(b"\0", i)
            sym = data[i:j].decode()
            i = j + 1
        elif op in (0x20, 0x70, 0x80):       # SET_DYLIB_ORDINAL_ULEB,
            _, i = _uleb(data, i)            # SET_SEGMENT_AND_OFFSET_ULEB,
        elif op == 0x60:                     # ADD_ADDR_ULEB; SET_ADDEND_SLEB
            _, i = _uleb(data, i)
        elif op in (0x90, 0xA0, 0xB0, 0xC0): # DO_BIND*
            if op == 0xA0:
                _, i = _uleb(data, i)
            elif op == 0xC0:
                _, i = _uleb(data, i)
                _, i = _uleb(data, i)
            if sym is not None and sym != "dyld_stub_binder":
                return True
        elif op == 0xD0 and imm == 0x00:     # THREADED SET_..._TABLE_SIZE
            _, i = _uleb(data, i)
        # 0x00 DONE, 0x10/0x30/0x50 immediate-only opcodes: nothing to skip
    return False


def run(path, max_insns=50_000_000):
    """Emulate main(); return its int result."""
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UcError
    from unicorn import arm64_const as A
    segs, entry = load(path)
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for _name, vmaddr, vmsize, blob in segs:
        base = vmaddr & ~0xFFF
        uc.mem_map(base, _page(vmaddr + vmsize - base))
        if blob:
            uc.mem_write(vmaddr, blob)
    uc.mem_map(STACK_TOP - STACK_SIZE, STACK_SIZE)
    uc.mem_map(SENTINEL, 0x1000)
    # Let the FP/SIMD unit run (CPACR_EL1.FPEN); otherwise the first d-reg
    # instruction traps.
    uc.reg_write(A.UC_ARM64_REG_CPACR_EL1,
                 uc.reg_read(A.UC_ARM64_REG_CPACR_EL1) | (3 << 20))
    uc.reg_write(A.UC_ARM64_REG_SP, STACK_TOP - 0x1000)
    uc.reg_write(A.UC_ARM64_REG_X29, 0)
    uc.reg_write(A.UC_ARM64_REG_X30, SENTINEL)
    uc.reg_write(A.UC_ARM64_REG_X0, 0)                 # argc
    uc.reg_write(A.UC_ARM64_REG_X1, 0)                 # argv
    try:
        uc.emu_start(entry, SENTINEL, count=max_insns)
    except UcError as e:
        pc = uc.reg_read(A.UC_ARM64_REG_PC)
        raise MachOError("emulation fault at pc=%#x: %s" % (pc, e))
    if uc.reg_read(A.UC_ARM64_REG_PC) != SENTINEL:
        raise MachOError("main did not return within %d instructions"
                         % max_insns)
    w0 = uc.reg_read(A.UC_ARM64_REG_X0) & 0xFFFFFFFF
    return w0 - (1 << 32) if w0 & 0x80000000 else w0


if __name__ == "__main__":
    try:
        rc = run(sys.argv[1])
    except MachOError as e:
        sys.stderr.write("macho_run: %s\n" % e)
        sys.exit(125)
    print(rc)
    sys.exit(rc & 0xFF)
