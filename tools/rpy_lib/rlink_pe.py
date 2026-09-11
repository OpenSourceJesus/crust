"""rlink_pe -- PE32+ (64-bit Windows) executable output for rlink.

rlink reads ELF objects, resolves symbols, lays sections out and applies
relocations. For Windows everything up to and including relocation is the
same: the objects are still ELF (rasm writes them, rlink reads them; nothing
outside Crust ever sees one). What differs is the image around them:

* **Imports.** A Windows executable reaches its C library through DLLs, by
  name, via an import table the loader fills in. Every symbol still
  undefined after the objects and archives are in is looked up in the known
  DLL exports (win64_imports.py, plus any DLL named on the link line). A
  function gets a 6-byte stub, `jmp QWORD PTR [rip + IAT slot]`, and the
  symbol resolves to the stub, so an ordinary `call printf` works unchanged.
  `__imp_NAME` resolves to the import-address-table slot itself, which is
  the only correct way to reach exported *data*: a stub is code and cannot
  stand in for a variable, so a direct reference to a data export is an
  error rather than a silently wrong address.

* **Layout.** Section RVAs are aligned to 4 KiB and file offsets to 512
  bytes, as the PE loader requires, and the headers occupy the first page.
  Input sections group into .text (executable), .rdata (read-only), .data
  (writable, including .got), .idata (the import table) and .bss.

* **Fixed base.** Crust's x86-64 back end addresses globals with absolute
  32-bit displacements (R_X86_64_32S), so the image must sit below 2 GiB.
  It is linked at 0x400000 with IMAGE_FILE_RELOCS_STRIPPED and without
  DYNAMIC_BASE: Windows loads it exactly there, no base relocations needed.
  ASLR would need RIP-relative addressing first; see WINDOWS.md.

Written in the same flat, RPython-friendly style as rlink itself.
"""

import rlink
import win64_imports

IMAGE_BASE = 0x400000
SECT_ALIGN = 0x1000
FILE_ALIGN = 0x200

# IMAGE_SCN_* characteristics
SCN_CODE = 0x00000020
SCN_IDATA = 0x00000040
SCN_UDATA = 0x00000080
SCN_EXEC = 0x20000000
SCN_READ = 0x40000000
SCN_WRITE = 0x80000000

THUNK_SIZE = 6          # FF 25 disp32: jmp QWORD PTR [rip + disp32]


def _align(v, a):
    return (v + a - 1) & ~(a - 1)


# --------------------------------------------------------------------------
# Reading a DLL's export table (to link against a DLL file directly)
# --------------------------------------------------------------------------
class DllExports(object):
    def __init__(self, name):
        self.name = name        # as the loader will look it up
        self.funcs = {}         # name -> True
        self.data = {}          # name -> True


def read_dll_exports(path_name, data):
    """Parse the export directory of PE image `data` (a list of byte ints).

    Each exported name is classified as data or code by whether its address
    lies in an executable section -- the same test used to derive
    win64_imports' tables -- so a DLL named on the link line needs no
    import library and no .def file. Forwarded exports are functions."""
    if len(data) < 64 or data[0] != 0x4D or data[1] != 0x5A:
        raise rlink.LinkError("%s: not a PE image (no MZ header)" % path_name)
    pe = rlink.u32(data, 0x3C)
    if rlink.u32(data, pe) != 0x4550:
        raise rlink.LinkError("%s: not a PE image" % path_name)
    nsec = rlink.u16(data, pe + 6)
    optsz = rlink.u16(data, pe + 20)
    opt = pe + 24
    if rlink.u16(data, opt) != 0x20B:
        raise rlink.LinkError("%s: not a 64-bit (PE32+) image" % path_name)
    exp_rva = rlink.u32(data, opt + 112)
    exp_size = rlink.u32(data, opt + 116)
    secs = []
    i = 0
    while i < nsec:
        o = opt + optsz + 40 * i
        vsz = rlink.u32(data, o + 8)
        va = rlink.u32(data, o + 12)
        rsz = rlink.u32(data, o + 16)
        raw = rlink.u32(data, o + 20)
        ch = rlink.u32(data, o + 36)
        span = vsz if vsz > rsz else rsz
        secs.append((va, span, raw, ch))
        i += 1

    def off(rva):
        for va, span, raw, ch in secs:
            if va <= rva and rva < va + span:
                return raw + rva - va
        raise rlink.LinkError("%s: RVA 0x%x is outside every section"
                              % (path_name, rva))

    def is_exec(rva):
        for va, span, raw, ch in secs:
            if va <= rva and rva < va + span:
                return (ch & SCN_EXEC) != 0
        return False

    if exp_rva == 0:
        raise rlink.LinkError("%s: DLL exports nothing" % path_name)
    e = off(exp_rva)
    dll_name = rlink.cstr(data, off(rlink.u32(data, e + 12)))
    ex = DllExports(dll_name)
    nnames = rlink.u32(data, e + 24)
    funcs_rva = rlink.u32(data, e + 28)
    names_rva = rlink.u32(data, e + 32)
    ords_rva = rlink.u32(data, e + 36)
    i = 0
    while i < nnames:
        nm = rlink.cstr(data, off(rlink.u32(data, off(names_rva) + 4 * i)))
        ordinal = rlink.u16(data, off(ords_rva) + 2 * i)
        frva = rlink.u32(data, off(funcs_rva) + 4 * ordinal)
        forwarded = exp_rva <= frva and frva < exp_rva + exp_size
        if forwarded or is_exec(frva):
            ex.funcs[nm] = True
        else:
            ex.data[nm] = True
        i += 1
    return ex


# --------------------------------------------------------------------------
# Import synthesis
# --------------------------------------------------------------------------
class ImportPlan(object):
    def __init__(self):
        self.dlls = []          # DLL names, in first-use order
        self.by_dll = {}        # dll -> list of imported names (sorted)
        self.slot_of = {}       # imported name -> (dll, index in its IAT)
        self.thunks = []        # function names that get a stub, in order
        self.thunk_holder = None
        self.idata_holder = None
        self.idata_rel = {}     # layout offsets inside .idata, see _size


def _lookup(ln, name):
    """(dll, is_data) for `name`: DLLs from the link line first, in order,
    then the built-in tables."""
    for ex in ln.pe_user_dlls:
        if name in ex.funcs:
            return (ex.name, False)
        if name in ex.data:
            return (ex.name, True)
    return win64_imports.lookup(name)


def _define(ln, name, holder, value):
    sym = rlink.InSymbol(name)
    sym.bind = rlink.STB_GLOBAL
    sym.shndx = 1
    sym.section = holder
    sym.value = value
    ln.globals[name] = sym
    if name in ln.undefined:
        del ln.undefined[name]
    ln.pe_synth_syms.append(sym)


def synthesize_imports(ln):
    """Turn every still-undefined symbol a known DLL exports into an import.

    Runs after the objects and archives are in and before layout, since the
    stubs and the import table take space in the image."""
    plan = ImportPlan()
    ln.pe_plan = plan
    wanted = {}             # imported name -> True
    thunk_for = {}          # function name -> True (needs a stub)
    errors = []
    for name in sorted(ln.undefined.keys()):
        g = ln.globals.get(name, None)
        if g is not None:
            continue
        base = name
        via_iat = False
        if name[0:6] == "__imp_":
            base = name[6:]
            via_iat = True
        hit = _lookup(ln, base)
        if hit is None:
            continue            # left for check_undefined to report
        dll = hit[0]
        is_data = hit[1]
        if is_data and not via_iat:
            errors.append(
                "`%s' is data exported by %s; a DLL's variables can only be "
                "reached through the import table: declare `extern T "
                "*__imp_%s;' and use `*__imp_%s'" % (base, dll, base, base))
            continue
        if base not in wanted:
            wanted[base] = True
            if dll not in plan.by_dll:
                plan.by_dll[dll] = []
                plan.dlls.append(dll)
            plan.by_dll[dll].append(base)
        if not via_iat:
            thunk_for[base] = True
    if len(errors) > 0:
        raise rlink.LinkError("; ".join(errors))
    for dll in plan.dlls:
        plan.by_dll[dll].sort()
        k = 0
        for nm in plan.by_dll[dll]:
            plan.slot_of[nm] = (dll, k)
            k += 1
    if len(plan.dlls) == 0:
        return

    # Stubs join .text, one per imported function actually called by name.
    plan.thunks = sorted(thunk_for.keys())
    if len(plan.thunks) > 0:
        th = rlink.InSection(None, ".text.imports", -1)
        th.type = rlink.SHT_PROGBITS
        th.flags = rlink.SHF_ALLOC | rlink.SHF_EXECINSTR
        th.align = 8
        th.size = THUNK_SIZE * len(plan.thunks)
        th.data = [0] * th.size
        th.keep = True
        th.out = ".text"
        ln._get_out(".text").inputs.append(th)
        ln._get_out(".text").flags |= th.flags
        plan.thunk_holder = th
        k = 0
        for nm in plan.thunks:
            _define(ln, nm, th, THUNK_SIZE * k)
            k += 1

    # The import table: directory, lookup tables, address tables, hint/name
    # entries and DLL names, in one writable section.
    idata = rlink.InSection(None, ".idata", -1)
    idata.type = rlink.SHT_PROGBITS
    idata.flags = rlink.SHF_ALLOC | rlink.SHF_WRITE
    idata.align = 8
    idata.size = _size_idata(plan)
    idata.data = [0] * idata.size
    idata.keep = True
    idata.out = ".idata"
    out = ln._get_out(".idata")
    out.inputs.append(idata)
    out.flags |= idata.flags
    out.align = 8
    plan.idata_holder = idata
    for nm in sorted(plan.slot_of.keys()):
        _define(ln, "__imp_" + nm, idata, _iat_off(plan, nm))


def _size_idata(plan):
    """Assign offsets inside .idata; return its size.

      [descriptor per DLL + null]      20 bytes each
      [ILT per DLL: n+1 entries]       8 bytes each   (lookup table)
      [IAT per DLL: n+1 entries]       8 bytes each   (filled by the loader)
      [hint/name entries]              u16 hint + name + NUL, even-padded
      [DLL names]                      NUL-terminated
    """
    rel = plan.idata_rel
    off = 20 * (len(plan.dlls) + 1)
    off = _align(off, 8)
    for dll in plan.dlls:
        rel["ilt:" + dll] = off
        off += 8 * (len(plan.by_dll[dll]) + 1)
    rel["iat_start"] = off
    for dll in plan.dlls:
        rel["iat:" + dll] = off
        off += 8 * (len(plan.by_dll[dll]) + 1)
    rel["iat_end"] = off
    for dll in plan.dlls:
        for nm in plan.by_dll[dll]:
            rel["hn:" + nm] = off
            off += _align(2 + len(nm) + 1, 2)
    for dll in plan.dlls:
        rel["dll:" + dll] = off
        off += len(dll) + 1
    return _align(off, 8)


def _iat_off(plan, nm):
    dll, k = plan.slot_of[nm]
    return plan.idata_rel["iat:" + dll] + 8 * k


def fill_imports(ln):
    """Write the import table and stub bodies once addresses are final."""
    plan = ln.pe_plan
    if plan is None or len(plan.dlls) == 0:
        return
    idata = plan.idata_holder
    rel = plan.idata_rel
    d = idata.data
    rva0 = idata.addr - ln.base
    i = 0
    for dll in plan.dlls:
        desc = 20 * i
        rlink.put(d, desc + 0, rva0 + rel["ilt:" + dll], 4)   # lookup table
        rlink.put(d, desc + 4, 0, 4)                          # timestamp
        rlink.put(d, desc + 8, 0, 4)                          # forwarders
        rlink.put(d, desc + 12, rva0 + rel["dll:" + dll], 4)  # DLL name
        rlink.put(d, desc + 16, rva0 + rel["iat:" + dll], 4)  # address table
        k = 0
        for nm in plan.by_dll[dll]:
            hn = rva0 + rel["hn:" + nm]
            rlink.put(d, rel["ilt:" + dll] + 8 * k, hn, 8)
            rlink.put(d, rel["iat:" + dll] + 8 * k, hn, 8)
            o = rel["hn:" + nm]
            rlink.put(d, o, 0, 2)                             # hint: none
            j = 0
            while j < len(nm):
                d[o + 2 + j] = ord(nm[j])
                j += 1
            k += 1
        o = rel["dll:" + dll]
        j = 0
        while j < len(dll):
            d[o + j] = ord(dll[j])
            j += 1
        i += 1
    th = plan.thunk_holder
    if th is not None:
        k = 0
        for nm in plan.thunks:
            at = th.addr + THUNK_SIZE * k
            slot = idata.addr + _iat_off(plan, nm)
            th.data[THUNK_SIZE * k] = 0xFF
            th.data[THUNK_SIZE * k + 1] = 0x25
            rlink.put(th.data, THUNK_SIZE * k + 2,
                      (slot - (at + THUNK_SIZE)) & 0xFFFFFFFF, 4)
            k += 1


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------
class PeSection(object):
    def __init__(self, name, chars):
        self.name = name
        self.chars = chars
        self.outs = []          # rlink OutSections placed here, in order
        self.rva = 0
        self.vsize = 0
        self.file_off = 0
        self.raw_size = 0
        self.nobits = False


def _pe_group(out):
    if out.name == ".idata":
        return ".idata"
    if out.nobits:
        return ".bss"
    if (out.flags & rlink.SHF_EXECINSTR) != 0:
        return ".text"
    if (out.flags & rlink.SHF_WRITE) != 0 or out.name == ".got":
        return ".data"
    return ".rdata"


_GROUPS = [(".text", SCN_CODE | SCN_EXEC | SCN_READ),
           (".rdata", SCN_IDATA | SCN_READ),
           (".data", SCN_IDATA | SCN_READ | SCN_WRITE),
           (".idata", SCN_IDATA | SCN_READ | SCN_WRITE),
           (".bss", SCN_UDATA | SCN_READ | SCN_WRITE)]


def headers_size(nsec):
    # DOS header (64) + "PE\0\0" (4) + COFF header (20) + optional (240)
    return _align(64 + 4 + 20 + 240 + 40 * nsec, FILE_ALIGN)


def layout(ln):
    """Assign RVAs and file offsets under PE alignment rules."""
    groups = []
    by = {}
    for gname, chars in _GROUPS:
        g = PeSection(gname, chars)
        by[gname] = g
    for out in ln.out_sections:
        if len(out.inputs) == 0:
            continue
        by[_pe_group(out)].outs.append(out)
    for gname, chars in _GROUPS:
        if len(by[gname].outs) > 0:
            groups.append(by[gname])
    ln.pe_sections = groups
    hsize = headers_size(len(groups))
    ln.pe_headers_size = hsize
    rva = _align(hsize, SECT_ALIGN)
    file_off = hsize
    for g in groups:
        g.nobits = g.name == ".bss"
        g.rva = rva
        g.file_off = file_off
        addr = ln.base + rva
        cur_off = file_off
        for out in g.outs:
            addr, cur_off = ln._place(out, addr, cur_off)
        g.vsize = addr - (ln.base + rva)
        if g.nobits:
            g.raw_size = 0
            g.file_off = 0
        else:
            g.raw_size = _align(g.vsize, FILE_ALIGN)
            file_off += g.raw_size
        rva = _align(rva + g.vsize, SECT_ALIGN)
        if g.name == ".text":
            ln.text_end = addr
    ln.pe_image_size = rva
    ln.data_end = ln.base + rva
    if not hasattr(ln, "text_end"):
        ln.text_end = ln.base
    # Synthetic symbols (stubs, __imp_ slots) are not in any object's symbol
    # table, so resolve() will not visit them; give them addresses here.
    for sym in ln.pe_synth_syms:
        sym.addr = sym.section.addr + sym.value
        sym.resolved = True


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------
def write(ln, subsystem):
    groups = ln.pe_sections
    total = ln.pe_headers_size
    for g in groups:
        if not g.nobits:
            end = g.file_off + g.raw_size
            if end > total:
                total = end
    img = [0] * total

    # DOS header: only e_magic and e_lfanew matter to the loader.
    img[0] = 0x4D
    img[1] = 0x5A
    rlink.put(img, 0x3C, 64, 4)
    pe = 64
    img[pe] = 0x50
    img[pe + 1] = 0x45

    code_size = 0
    idata_size = 0
    udata_size = 0
    base_of_code = 0
    for g in groups:
        if g.name == ".text":
            code_size += g.raw_size
            base_of_code = g.rva
        elif g.nobits:
            udata_size += _align(g.vsize, FILE_ALIGN)
        else:
            idata_size += g.raw_size

    # COFF file header
    c = pe + 4
    rlink.put(img, c + 0, 0x8664, 2)            # AMD64
    rlink.put(img, c + 2, len(groups), 2)
    rlink.put(img, c + 4, 0, 4)                 # timestamp: reproducible
    rlink.put(img, c + 16, 240, 2)              # optional header size
    # RELOCS_STRIPPED | EXECUTABLE_IMAGE | LARGE_ADDRESS_AWARE
    rlink.put(img, c + 18, 0x0001 | 0x0002 | 0x0020, 2)

    # Optional header (PE32+)
    o = c + 20
    rlink.put(img, o + 0, 0x20B, 2)
    img[o + 2] = 1                              # linker version 1.0
    rlink.put(img, o + 4, code_size, 4)
    rlink.put(img, o + 8, idata_size, 4)
    rlink.put(img, o + 12, udata_size, 4)
    rlink.put(img, o + 16, ln.entry - ln.base, 4)
    rlink.put(img, o + 20, base_of_code, 4)
    rlink.put(img, o + 24, ln.base, 8)
    rlink.put(img, o + 32, SECT_ALIGN, 4)
    rlink.put(img, o + 36, FILE_ALIGN, 4)
    rlink.put(img, o + 40, 6, 2)                # OS version 6.0 (Vista+)
    rlink.put(img, o + 48, 6, 2)                # subsystem version 6.0
    rlink.put(img, o + 56, ln.pe_image_size, 4)
    rlink.put(img, o + 60, ln.pe_headers_size, 4)
    rlink.put(img, o + 68, subsystem, 2)        # 3 = console, 2 = GUI
    # NX_COMPAT | TERMINAL_SERVER_AWARE; deliberately not DYNAMIC_BASE
    rlink.put(img, o + 70, 0x0100 | 0x8000, 2)
    rlink.put(img, o + 72, 0x200000, 8)         # stack reserve: 2 MiB
    rlink.put(img, o + 80, 0x1000, 8)           # stack commit
    rlink.put(img, o + 88, 0x100000, 8)         # heap reserve
    rlink.put(img, o + 96, 0x1000, 8)           # heap commit
    rlink.put(img, o + 108, 16, 4)              # data directory count
    plan = ln.pe_plan
    if plan is not None and len(plan.dlls) > 0:
        idata = plan.idata_holder
        rva = idata.addr - ln.base
        dd = o + 112
        rlink.put(img, dd + 8, rva, 4)                            # import
        rlink.put(img, dd + 12, 20 * (len(plan.dlls) + 1), 4)
        rel = plan.idata_rel
        rlink.put(img, dd + 96, rva + rel["iat_start"], 4)        # IAT
        rlink.put(img, dd + 100, rel["iat_end"] - rel["iat_start"], 4)

    # Section headers
    s = o + 240
    for g in groups:
        j = 0
        while j < len(g.name) and j < 8:
            img[s + j] = ord(g.name[j])
            j += 1
        rlink.put(img, s + 8, g.vsize, 4)
        rlink.put(img, s + 12, g.rva, 4)
        rlink.put(img, s + 16, g.raw_size, 4)
        rlink.put(img, s + 20, g.file_off, 4)
        rlink.put(img, s + 36, g.chars, 4)
        s += 40

    # Section contents
    for g in groups:
        if g.nobits:
            continue
        for out in g.outs:
            for sec in out.inputs:
                if sec.type == rlink.SHT_NOBITS:
                    continue
                start = g.file_off + (sec.addr - (ln.base + g.rva))
                k = 0
                n = len(sec.data)
                while k < n:
                    img[start + k] = sec.data[k]
                    k += 1
    return img


def link_pe(ln, subsystem=3):
    """The rlink.Linker.link pipeline, with PE layout and output."""
    ln.pull_archives()
    ln.collect_sections()
    ln.allocate_commons()
    synthesize_imports(ln)
    ln.scan_got()
    layout(ln)
    ln.resolve()
    ln.check_undefined()
    ln.fill_got()
    ln.relocate()
    fill_imports(ln)
    ln.find_entry()
    return write(ln, subsystem)


def new_linker():
    """An rlink.Linker configured for a Windows executable."""
    ln = rlink.Linker()
    ln.base = IMAGE_BASE
    ln.entry_name = "mainCRTStartup"
    ln.pe_user_dlls = []
    ln.pe_synth_syms = []
    ln.pe_plan = None
    return ln


def add_dll(ln, path_name, data):
    """Link against DLL file `data`: its exports become importable."""
    ln.pe_user_dlls.append(read_dll_exports(path_name, data))
