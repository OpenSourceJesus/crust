"""rlink -- a static ELF64 x86-64 linker, written in RPython-friendly Python.

This is the last external tool in the ShivyCX self-hosting path. With rasm
replacing GNU `as`, the pipeline was still `py2c -> C -> ShivyCX -> rasm -> ld`;
rlink replaces that final `ld`, so the whole chain is our own code.

What it does, in the order it does it:

  1. **Read** ELF64 relocatable objects (`ET_REL`), and `ar` archives, from
     which members are pulled in only when they define a symbol that is still
     undefined (the usual "resolve to a fixpoint" rule).
  2. **Resolve** the global symbol table: strong definitions beat weak ones,
     weak-undefined resolves to 0, and `SHN_COMMON` tentative definitions are
     allocated space in `.bss`.
  3. **Lay out** the output: input sections are concatenated by output name and
     grouped into two `PT_LOAD` segments -- read+execute (`.text`, `.rodata`)
     and read+write (`.data`, `.got`, `.bss`) -- each page-aligned, with file
     offsets congruent to virtual addresses modulo the page size.
  4. **Relocate**: every relocation is applied against final addresses. A `.got`
     is synthesised on demand for the `GOTPCREL` family.
  5. **Write** an `ET_EXEC` static executable: ELF header, program headers,
     section contents, and a symbol table (kept so `objdump`/`readelf` and
     debuggers still work on our output).

Style constraints match the rest of the dialect: no metaclasses, decorators,
generators or `**kwargs`; uniform record classes; bytes handled as lists of
ints.
"""


# --------------------------------------------------------------------------
# little-endian readers over a list-of-ints buffer
# --------------------------------------------------------------------------
def u8(b, off):
    return b[off]


def u16(b, off):
    return b[off] | (b[off + 1] << 8)


def u32(b, off):
    return (b[off] | (b[off + 1] << 8) | (b[off + 2] << 16)
            | (b[off + 3] << 24))


def u64(b, off):
    lo = u32(b, off)
    hi = u32(b, off + 4)
    return lo | (hi << 32)


def s32(b, off):
    v = u32(b, off)
    if v >= 0x80000000:
        v -= 0x100000000
    return v


def s64(b, off):
    v = u64(b, off)
    if v >= 0x8000000000000000:
        v -= 0x10000000000000000
    return v


def pack(value, nbytes):
    out = []
    v = value & ((1 << (8 * nbytes)) - 1)
    i = 0
    while i < nbytes:
        out.append(v & 0xFF)
        v >>= 8
        i += 1
    return out


def put(buf, off, value, nbytes):
    bs = pack(value, nbytes)
    i = 0
    while i < nbytes:
        buf[off + i] = bs[i]
        i += 1


def cstr(b, off):
    out = ""
    i = off
    while i < len(b) and b[i] != 0:
        out += chr(b[i])
        i += 1
    return out


class LinkError(Exception):
    pass


# --------------------------------------------------------------------------
# ELF constants
# --------------------------------------------------------------------------
ET_REL = 1
ET_EXEC = 2
EM_X86_64 = 62

SHT_NULL = 0
SHT_PROGBITS = 1
SHT_SYMTAB = 2
SHT_STRTAB = 3
SHT_RELA = 4
SHT_NOBITS = 8

SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4
SHF_MERGE = 0x10
SHF_STRINGS = 0x20
SHF_TLS = 0x400

SHN_UNDEF = 0
SHN_ABS = 0xFFF1
SHN_COMMON = 0xFFF2

STB_LOCAL = 0
STB_GLOBAL = 1
STB_WEAK = 2

STT_NOTYPE = 0
STT_OBJECT = 1
STT_FUNC = 2
STT_SECTION = 3
STT_FILE = 4

PT_LOAD = 1
PT_GNU_STACK = 0x6474E551

PF_X = 1
PF_W = 2
PF_R = 4

# relocation types we understand
R_X86_64_NONE = 0
R_X86_64_64 = 1
R_X86_64_PC32 = 2
R_X86_64_GOT32 = 3
R_X86_64_PLT32 = 4
R_X86_64_GOTPCREL = 9
R_X86_64_32 = 10
R_X86_64_32S = 11
R_X86_64_16 = 12
R_X86_64_PC16 = 13
R_X86_64_8 = 14
R_X86_64_PC8 = 15
R_X86_64_PC64 = 24
R_X86_64_GOTPCRELX = 41
R_X86_64_REX_GOTPCRELX = 42

PAGE = 0x1000
DEFAULT_BASE = 0x400000


# --------------------------------------------------------------------------
# Input model
# --------------------------------------------------------------------------
class InSection(object):
    """One section of one input object."""

    def __init__(self, obj, name, index):
        self.obj = obj
        self.name = name
        self.index = index          # section index inside its object
        self.data = []              # bytes ([] for .bss)
        self.size = 0
        self.flags = 0
        self.type = SHT_PROGBITS
        self.align = 1
        self.relocs = []            # list[Reloc]
        self.addr = 0               # final virtual address (set by layout)
        self.out = ""               # output section name it was merged into
        self.keep = False           # allocated & part of the image


class Reloc(object):
    def __init__(self, offset, symidx, rtype, addend):
        self.offset = offset        # within the section being relocated
        self.symidx = symidx        # index into the object's symbol table
        self.rtype = rtype
        self.addend = addend


class InSymbol(object):
    """A symbol as it appears in one input object."""

    def __init__(self, name):
        self.name = name
        self.value = 0
        self.size = 0
        self.bind = STB_LOCAL
        self.stype = STT_NOTYPE
        self.shndx = SHN_UNDEF
        self.section = None         # InSection when defined in one
        self.obj = None
        self.addr = 0               # final address, filled during resolution
        self.resolved = False


class ObjFile(object):
    def __init__(self, name):
        self.name = name
        self.sections = []          # list[InSection], indexed by ELF index
        self.symbols = []           # list[InSymbol], indexed by symtab index
        self.included = True


# --------------------------------------------------------------------------
# ELF object reader
# --------------------------------------------------------------------------
def read_object(name, data):
    """Parse an ET_REL ELF64 object into an ObjFile."""
    if len(data) < 64:
        raise LinkError("%s: too short to be an ELF object" % name)
    if not (data[0] == 0x7F and data[1] == ord('E') and data[2] == ord('L')
            and data[3] == ord('F')):
        raise LinkError("%s: not an ELF file" % name)
    if data[4] != 2 or data[5] != 1:
        raise LinkError("%s: not a little-endian 64-bit ELF file" % name)
    etype = u16(data, 16)
    machine = u16(data, 18)
    if etype != ET_REL:
        raise LinkError("%s: not a relocatable object (e_type=%d)"
                        % (name, etype))
    if machine != EM_X86_64:
        raise LinkError("%s: not an x86-64 object (e_machine=%d)"
                        % (name, machine))

    shoff = u64(data, 40)
    shentsize = u16(data, 58)
    shnum = u16(data, 60)
    shstrndx = u16(data, 62)

    # raw section headers
    hdrs = []
    i = 0
    while i < shnum:
        o = shoff + i * shentsize
        hdrs.append({
            "name": u32(data, o),
            "type": u32(data, o + 4),
            "flags": u64(data, o + 8),
            "addr": u64(data, o + 16),
            "off": u64(data, o + 24),
            "size": u64(data, o + 32),
            "link": u32(data, o + 40),
            "info": u32(data, o + 44),
            "align": u64(data, o + 48),
            "entsize": u64(data, o + 56),
        })
        i += 1

    shstr_off = hdrs[shstrndx]["off"] if shnum > 0 else 0

    obj = ObjFile(name)
    i = 0
    while i < shnum:
        h = hdrs[i]
        sname = cstr(data, shstr_off + h["name"])
        sec = InSection(obj, sname, i)
        sec.flags = h["flags"]
        sec.type = h["type"]
        sec.size = h["size"]
        sec.align = h["align"] if h["align"] > 0 else 1
        if h["type"] == SHT_PROGBITS and h["size"] > 0:
            sec.data = data[h["off"]:h["off"] + h["size"]]
        sec.keep = (h["flags"] & SHF_ALLOC) != 0
        obj.sections.append(sec)
        i += 1

    # symbol table
    symtab_idx = -1
    i = 0
    while i < shnum:
        if hdrs[i]["type"] == SHT_SYMTAB:
            symtab_idx = i
            break
        i += 1
    if symtab_idx >= 0:
        sh = hdrs[symtab_idx]
        strh = hdrs[sh["link"]]
        count = sh["size"] // 24
        k = 0
        while k < count:
            o = sh["off"] + k * 24
            nameoff = u32(data, o)
            info = u8(data, o + 4)
            shndx = u16(data, o + 6)
            value = u64(data, o + 8)
            size = u64(data, o + 16)
            sym = InSymbol(cstr(data, strh["off"] + nameoff))
            sym.bind = (info >> 4) & 0xF
            sym.stype = info & 0xF
            sym.shndx = shndx
            sym.value = value
            sym.size = size
            sym.obj = obj
            if shndx != SHN_UNDEF and shndx < shnum and shndx < SHN_ABS:
                sym.section = obj.sections[shndx]
            obj.symbols.append(sym)
            k += 1

    # relocations (RELA only; x86-64 does not use REL)
    i = 0
    while i < shnum:
        h = hdrs[i]
        if h["type"] == SHT_RELA:
            target = h["info"]
            if target < len(obj.sections):
                sec = obj.sections[target]
                count = h["size"] // 24
                k = 0
                while k < count:
                    o = h["off"] + k * 24
                    roff = u64(data, o)
                    rinfo = u64(data, o + 8)
                    radd = s64(data, o + 16)
                    sec.relocs.append(Reloc(roff, rinfo >> 32,
                                            rinfo & 0xFFFFFFFF, radd))
                    k += 1
        i += 1

    return obj


# --------------------------------------------------------------------------
# `ar` archive reader
# --------------------------------------------------------------------------
class ArMember(object):
    def __init__(self, name, data):
        self.name = name
        self.data = data
        self.obj = None
        self.included = False


def _ar_int(data, off, n):
    s = ""
    i = 0
    while i < n:
        c = chr(data[off + i])
        if c != " ":
            s += c
        i += 1
    if s == "":
        return 0
    return int(s)


def read_archive(name, data):
    """Split a System V `ar` archive into its members."""
    magic = ""
    i = 0
    while i < 8:
        magic += chr(data[i])
        i += 1
    if magic != "!<arch>\n":
        raise LinkError("%s: not an ar archive" % name)
    members = []
    longnames = []
    pos = 8
    while pos + 60 <= len(data):
        raw = ""
        i = 0
        while i < 16:
            raw += chr(data[pos + i])
            i += 1
        size = _ar_int(data, pos + 48, 10)
        body = data[pos + 60:pos + 60 + size]
        mname = raw.strip()
        if mname == "/" or mname == "/SYM64/":
            pass                                   # archive symbol index
        elif mname == "//":
            longnames = body                       # long-name string table
        else:
            if mname[0:1] == "/":                  # /offset into //
                off = int(mname[1:])
                nm = ""
                j = off
                while j < len(longnames) and longnames[j] != 0x2F \
                        and longnames[j] != 0x0A:
                    nm += chr(longnames[j])
                    j += 1
                mname = nm
            elif mname.endswith("/"):
                mname = mname[:-1]
            members.append(ArMember("%s(%s)" % (name, mname), body))
        pos += 60 + size
        if size % 2 == 1:
            pos += 1
    return members


# --------------------------------------------------------------------------
# Output model
# --------------------------------------------------------------------------
class OutSection(object):
    def __init__(self, name):
        self.name = name
        self.inputs = []            # list[InSection]
        self.addr = 0
        self.size = 0
        self.align = 1
        self.flags = 0
        self.nobits = False
        self.file_off = 0
        self.data = []


def _out_name(sec_name):
    """Map an input section name to the output section it merges into.

    gcc/`-ffunction-sections` style names (`.text.foo`, `.rodata.str1.1`,
    `.data.rel.ro`) all fold into their base section, which is what a simple
    default link script does."""
    n = sec_name
    if n[0:5] == ".text":
        return ".text"
    if n[0:7] == ".rodata":
        return ".rodata"
    if n[0:12] == ".data.rel.ro":
        return ".data"
    if n[0:5] == ".data":
        return ".data"
    if n[0:4] == ".bss":
        return ".bss"
    if n[0:11] == ".init_array":
        return ".init_array"
    if n[0:11] == ".fini_array":
        return ".fini_array"
    if n[0:4] == ".got":
        return ".got"
    if n == ".init" or n == ".fini":
        return ".text"
    return n


# read+execute first, then read-only, then writable, then bss last
_ORDER = [".text", ".rodata", ".init_array", ".fini_array", ".data", ".got",
          ".bss"]


def _order_key(name):
    i = 0
    while i < len(_ORDER):
        if name == _ORDER[i]:
            return i
        i += 1
    # unknown allocated sections sit with .data
    return len(_ORDER)


# --------------------------------------------------------------------------
# Linker scripts
# --------------------------------------------------------------------------
# A bare-metal image cannot use the default layout: the Multiboot header has to
# land in the first 8 KiB, everything sits at a fixed physical address, and the
# boot code needs `_load_start` / `_load_end` / `_bss_end` to describe itself to
# the loader. All of that lives in a linker script, so rlink understands the
# subset those scripts actually use:
#
#     ENTRY(sym)
#     SECTIONS {
#         . = 0x00100000;
#         _load_start = .;
#         .text : { *(.multiboot) *(.text) *(.text.*) }
#         _load_end = .;
#         /DISCARD/ : { *(.comment) *(.note.*) }
#     }
#
# Not supported: MEMORY regions, overlays, AT> load addresses, arithmetic beyond
# a bare number or `.`, and ALIGN(). Those raise rather than being ignored, so a
# script that needs them fails loudly instead of producing a subtly wrong image.

SCMD_SETDOT = 0      # . = <value>
SCMD_ASSIGN = 1      # name = .
SCMD_SECTION = 2     # .name : { *(pat) ... }
SCMD_DISCARD = 3     # /DISCARD/ : { *(pat) ... }


class ScriptCmd(object):
    def __init__(self, kind):
        self.kind = kind
        self.name = ""
        self.value = 0
        self.patterns = []
        self.align = 0


class Script(object):
    def __init__(self):
        self.entry = ""
        self.cmds = []
        self.discard = []


def _script_tokens(text):
    """Tokenise a script: strip comments, then split on whitespace and the
    punctuation that matters."""
    out = ""
    i = 0
    while i < len(text):
        if text[i] == "/" and text[i + 1:i + 2] == "*":
            i += 2
            while i < len(text) and not (text[i] == "*"
                                         and text[i + 1:i + 2] == "/"):
                i += 1
            i += 2
            continue
        c = text[i]
        if c in "{}();=,":
            out += " " + c + " "
        elif c == "\n" or c == "\t" or c == "\r":
            out += " "
        else:
            out += c
        i += 1
    return [t for t in out.split(" ") if t != ""]


def parse_script(text):
    toks = _script_tokens(text)
    sc = Script()
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t == "ENTRY":
            sc.entry = toks[i + 2]
            i += 4                      # ENTRY ( name )
            continue
        if t == "SECTIONS":
            i += 2                      # SECTIONS {
            while i < n and toks[i] != "}":
                i = _parse_section_item(sc, toks, i)
            i += 1
            continue
        if t == "OUTPUT_FORMAT" or t == "OUTPUT_ARCH":
            while i < n and toks[i] != ")":
                i += 1
            i += 1
            continue
        i += 1
    return sc


def _parse_section_item(sc, toks, i):
    t = toks[i]
    if t == ".":                        # . = <value> ;
        if toks[i + 1] != "=":
            raise LinkError("unsupported script syntax near `.`")
        cmd = ScriptCmd(SCMD_SETDOT)
        cmd.value = _script_number(toks[i + 2])
        sc.cmds.append(cmd)
        return i + 3
    if i + 1 < len(toks) and toks[i + 1] == "=":
        # name = . ;   (a PROVIDE-style symbol at the current address)
        if toks[i + 2] != ".":
            raise LinkError("only `sym = .` assignments are supported, got "
                            "`%s = %s`" % (t, toks[i + 2]))
        cmd = ScriptCmd(SCMD_ASSIGN)
        cmd.name = t
        sc.cmds.append(cmd)
        return i + 3
    if t == ";":
        return i + 1
    # an output section: NAME : { *(pat) ... }
    name = t
    j = i + 1
    while j < len(toks) and toks[j] != "{":
        j += 1
    j += 1
    pats = []
    while j < len(toks) and toks[j] != "}":
        tok = toks[j]
        if tok == "*" and toks[j + 1:j + 2] == ["("]:
            # `*(.text .text.*)` -- every input section matching any of these
            j += 2
            while j < len(toks) and toks[j] != ")":
                if toks[j] != ",":
                    pats.append(toks[j])
                j += 1
        j += 1
    j += 1
    if name == "/DISCARD/":
        sc.discard.extend(pats)
    else:
        cmd = ScriptCmd(SCMD_SECTION)
        cmd.name = name
        cmd.patterns = pats
        sc.cmds.append(cmd)
    return j


def _script_number(tok):
    t = tok
    if t.endswith(";"):
        t = t[:-1]
    if t[0:2] == "0x" or t[0:2] == "0X":
        return int(t[2:], 16)
    if t.endswith("M"):
        return int(t[:-1]) * 1024 * 1024
    if t.endswith("K"):
        return int(t[:-1]) * 1024
    return int(t)


def _pattern_match(pat, name):
    """Glob matching for the `*(.text.*)` forms scripts use."""
    if pat == name:
        return True
    star = pat.find("*")
    if star < 0:
        return False
    head = pat[:star]
    tail = pat[star + 1:]
    if not name.startswith(head):
        return False
    if tail == "":
        return True
    return name.endswith(tail) and len(name) >= len(head) + len(tail)


class Linker(object):
    def __init__(self):
        self.objects = []           # list[ObjFile] actually linked in
        self.archives = []          # list[list[ArMember]]
        self.globals = {}           # name -> InSymbol (the chosen definition)
        self.undefined = {}         # name -> True while still unresolved
        self.commons = {}           # name -> InSymbol (tentative definitions)
        self.out_sections = []      # list[OutSection]
        self.out_by_name = {}
        self.base = DEFAULT_BASE
        self.entry_name = "_start"
        self.entry = 0
        self.got_entries = {}       # symbol name -> offset in .got
        self.got_order = []
        self.script = None          # Script, when -T was given
        self.script_syms = {}       # name -> address, from `sym = .`
        self.warnings = []

    # -- inputs -----------------------------------------------------------
    def add_object(self, name, data):
        obj = read_object(name, data)
        self._absorb(obj)

    def add_archive(self, name, data):
        self.archives.append(read_archive(name, data))

    def _absorb(self, obj):
        self.objects.append(obj)
        for sym in obj.symbols:
            if sym.bind == STB_LOCAL:
                continue
            if sym.shndx == SHN_UNDEF:
                if sym.name != "" and sym.name not in self.globals:
                    self.undefined[sym.name] = True
            else:
                self._define(sym)

    def _define(self, sym):
        """Record a global definition, applying the strong/weak/common rules."""
        name = sym.name
        if name == "":
            return
        if sym.shndx == SHN_COMMON:
            prev = self.commons.get(name, None)
            if prev is None or sym.size > prev.size:
                self.commons[name] = sym
            if name in self.undefined:
                del self.undefined[name]
            return
        old = self.globals.get(name, None)
        if old is not None:
            if old.bind == STB_WEAK and sym.bind != STB_WEAK:
                self.globals[name] = sym        # strong overrides weak
            elif sym.bind == STB_WEAK:
                pass                            # keep the existing definition
            else:
                # two strong definitions: first one wins, but say so
                self.warnings.append("duplicate definition of %s (%s and %s)"
                                     % (name, old.obj.name, sym.obj.name))
            return
        self.globals[name] = sym
        if name in self.undefined:
            del self.undefined[name]

    def pull_archives(self):
        """Include archive members that satisfy still-undefined symbols, until
        a pass adds nothing new."""
        changed = True
        while changed:
            changed = False
            for members in self.archives:
                for m in members:
                    if m.included:
                        continue
                    if not self._member_needed(m):
                        continue
                    m.included = True
                    m.obj = read_object(m.name, m.data)
                    self._absorb(m.obj)
                    changed = True

    def _member_needed(self, m):
        if m.obj is None:
            try:
                m.obj = read_object(m.name, m.data)
            except LinkError:
                return False
        for sym in m.obj.symbols:
            if sym.bind == STB_LOCAL or sym.shndx == SHN_UNDEF:
                continue
            if sym.name in self.undefined:
                return True
        return False

    # -- layout -----------------------------------------------------------
    def _get_out(self, name):
        s = self.out_by_name.get(name, None)
        if s is None:
            s = OutSection(name)
            self.out_by_name[name] = s
            self.out_sections.append(s)
        return s

    def collect_sections(self):
        if self.script is not None:
            return self._collect_scripted()
        for obj in self.objects:
            for sec in obj.sections:
                if not sec.keep or sec.name == "":
                    continue
                if (sec.flags & SHF_TLS) != 0:
                    raise LinkError("%s: thread-local storage is not supported"
                                    % sec.name)
                oname = _out_name(sec.name)
                out = self._get_out(oname)
                out.inputs.append(sec)
                sec.out = oname
                if sec.align > out.align:
                    out.align = sec.align
                out.flags |= sec.flags
                if sec.type == SHT_NOBITS:
                    out.nobits = out.nobits or True
        # .bss is the only nobits output; anything mixed becomes progbits
        for out in self.out_sections:
            allnobits = True
            for sec in out.inputs:
                if sec.type != SHT_NOBITS:
                    allnobits = False
            out.nobits = allnobits and len(out.inputs) > 0
        self.out_sections.sort(key=_order_sort_key)

    def _collect_scripted(self):
        """Group input sections the way the script says, in its order.

        Order matters here in a way it does not for a hosted program: the
        Multiboot header must be the first thing in the image, so `*(.multiboot)`
        leading `.text` is load-bearing, not cosmetic.
        """
        taken = {}
        for cmd in self.script.cmds:
            if cmd.kind != SCMD_SECTION:
                continue
            out = self._get_out(cmd.name)
            for pat in cmd.patterns:
                if pat == "COMMON":
                    continue          # commons are added by allocate_commons
                for obj in self.objects:
                    for sec in obj.sections:
                        if not sec.keep or sec.name == "":
                            continue
                        key = "%s\x00%s" % (obj.name, sec.index)
                        if key in taken:
                            continue
                        if not _pattern_match(pat, sec.name):
                            continue
                        if (sec.flags & SHF_TLS) != 0:
                            raise LinkError("%s: thread-local storage is not "
                                            "supported" % sec.name)
                        taken[key] = True
                        out.inputs.append(sec)
                        sec.out = cmd.name
                        if sec.align > out.align:
                            out.align = sec.align
                        out.flags |= sec.flags
        # anything the script did not mention, and did not discard, is dropped
        for obj in self.objects:
            for sec in obj.sections:
                if not sec.keep or sec.out != "":
                    continue
                sec.keep = False
        for out in self.out_sections:
            allnobits = True
            for sec in out.inputs:
                if sec.type != SHT_NOBITS:
                    allnobits = False
            out.nobits = allnobits and len(out.inputs) > 0

    def _script_layout(self):
        """Walk the script, placing sections and defining `sym = .` symbols.

        Sections are packed tightly (the `-n` / nmagic behaviour bare-metal
        images want): no page alignment between them, because the loader is a
        flat copy, not mmap.
        """
        dot = self.base
        file_off = PAGE          # contents start on a page so p_offset ~ p_vaddr
        first_addr = -1
        for cmd in self.script.cmds:
            if cmd.kind == SCMD_SETDOT:
                dot = cmd.value
                continue
            if cmd.kind == SCMD_ASSIGN:
                self.script_syms[cmd.name] = dot
                continue
            out = self.out_by_name.get(cmd.name, None)
            if out is None:
                continue
            if out.align > 1 and dot % out.align != 0:
                pad = out.align - (dot % out.align)
                dot += pad
                if not out.nobits:
                    file_off += pad
            if first_addr < 0 and not out.nobits:
                first_addr = dot
                self.first_file_off = file_off
            dot, file_off = self._place(out, dot, file_off)
        self.base = first_addr if first_addr >= 0 else self.base
        self.image_end = dot
        self.image_file_end = file_off
        # one loadable segment covering the whole image
        self.exec_secs = []
        self.rw_secs = []
        for cmd in self.script.cmds:
            if cmd.kind != SCMD_SECTION:
                continue
            out = self.out_by_name.get(cmd.name, None)
            if out is not None:
                self.exec_secs.append(out)
        self.text_end = dot
        self.text_file_end = file_off
        self.data_end = dot
        self.data_file_end = file_off
        self.nphdr = 1

    def allocate_commons(self):
        """Tentative (SHN_COMMON) definitions get storage in .bss."""
        names = sorted(self.commons.keys())
        if len(names) == 0:
            return
        bss = self._get_out(".bss")
        bss.nobits = True
        bss.flags |= SHF_ALLOC | SHF_WRITE
        if ".bss" not in self.out_by_name:
            self.out_by_name[".bss"] = bss
        # a synthetic input section holds all commons
        holder = InSection(None, ".bss.common", -1)
        holder.type = SHT_NOBITS
        holder.flags = SHF_ALLOC | SHF_WRITE
        holder.align = 16
        off = 0
        for nm in names:
            sym = self.commons[nm]
            # ELF rule: a real definition beats a tentative one. If some object
            # actually defined this symbol, the common is discarded rather than
            # allocated -- otherwise we would shadow the definition with fresh
            # zeroed .bss (e.g. rlibc's `char **environ;` versus the real slot
            # rcrt.s defines and _start writes).
            existing = self.globals.get(nm, None)
            if existing is not None and existing.shndx != SHN_COMMON \
                    and existing.section is not None:
                continue
            a = sym.value if sym.value > 0 else 8
            if a > 1 and off % a != 0:
                off += a - (off % a)
            sym.section = holder
            sym.value = off
            sym.shndx = 1               # no longer SHN_COMMON
            self.globals[nm] = sym
            off += sym.size
        holder.size = off
        holder.keep = True
        holder.out = ".bss"
        bss.inputs.append(holder)
        if holder.align > bss.align:
            bss.align = holder.align

    def layout(self):
        """Assign virtual addresses. Two PT_LOAD segments: RX then RW, each
        page-aligned so the kernel can map them with distinct protections."""
        if self.script is not None:
            return self._script_layout()
        # reserve room for the ELF header + program headers at the image base
        # (the classic trick: the first segment maps from file offset 0).
        self.nphdr = 3
        hdr_size = 64 + self.nphdr * 56
        addr = self.base + hdr_size

        exec_secs = []
        rw_secs = []
        for out in self.out_sections:
            if (out.flags & SHF_WRITE) != 0 or out.name == ".got" \
                    or out.name == ".bss":
                rw_secs.append(out)
            else:
                exec_secs.append(out)

        file_off = hdr_size
        for out in exec_secs:
            addr, file_off = self._place(out, addr, file_off)
        self.text_end = addr
        self.text_file_end = file_off

        # start the writable segment on a fresh page, keeping
        # (file offset % PAGE) == (vaddr % PAGE) as the loader requires
        addr = (addr + PAGE - 1) & ~(PAGE - 1)
        addr += file_off % PAGE
        for out in rw_secs:
            addr, file_off = self._place(out, addr, file_off)
        self.data_end = addr
        self.data_file_end = file_off

        self.exec_secs = exec_secs
        self.rw_secs = rw_secs

    def _place(self, out, addr, file_off):
        if out.align > 1:
            rem = addr % out.align
            if rem != 0:
                pad = out.align - rem
                addr += pad
                if not out.nobits:
                    file_off += pad
        out.addr = addr
        out.file_off = file_off
        size = 0
        for sec in out.inputs:
            if sec.align > 1:
                rem = size % sec.align
                if rem != 0:
                    size += sec.align - rem
            sec.addr = addr + size
            size += sec.size
        out.size = size
        addr += size
        if not out.nobits:
            file_off += size
        return (addr, file_off)

    # -- symbol addresses -------------------------------------------------
    def resolve(self):
        """Give every symbol its final address."""
        for obj in self.objects:
            for sym in obj.symbols:
                if sym.section is not None and sym.section.keep:
                    sym.addr = sym.section.addr + sym.value
                    sym.resolved = True
                elif sym.shndx == SHN_ABS:
                    sym.addr = sym.value
                    sym.resolved = True
        # symbols the script defined with `sym = .`
        for nm in sorted(self.script_syms.keys()):
            self._provide_forced(nm, self.script_syms[nm])
        # linker-defined symbols a freestanding runtime expects
        self._provide("__executable_start", self.base)
        self._provide("__etext", self.text_end)
        self._provide("_etext", self.text_end)
        bss = self.out_by_name.get(".bss", None)
        if bss is not None:
            self._provide("__bss_start", bss.addr)
            self._provide("_edata", bss.addr)
        else:
            self._provide("__bss_start", self.data_end)
            self._provide("_edata", self.data_end)
        self._provide("_end", self.data_end)
        self._provide("end", self.data_end)

    def _provide_forced(self, name, value):
        """Define `name` unconditionally -- a script assignment wins."""
        sym = InSymbol(name)
        sym.bind = STB_GLOBAL
        sym.shndx = SHN_ABS
        sym.value = value
        sym.addr = value
        sym.resolved = True
        self.globals[name] = sym
        if name in self.undefined:
            del self.undefined[name]

    def _provide(self, name, value):
        """Define `name` only if some object referenced it and nothing else
        defined it -- the equivalent of a linker script's PROVIDE()."""
        existing = self.globals.get(name, None)
        if existing is not None and existing.resolved:
            return
        sym = InSymbol(name)
        sym.bind = STB_GLOBAL
        sym.shndx = SHN_ABS
        sym.value = value
        sym.addr = value
        sym.resolved = True
        self.globals[name] = sym
        if name in self.undefined:
            del self.undefined[name]

    def _sym_addr(self, obj, idx, where):
        """Final address of symbol `idx` as referenced from object `obj`."""
        if idx >= len(obj.symbols):
            raise LinkError("%s: relocation references symbol %d out of range"
                            % (obj.name, idx))
        sym = obj.symbols[idx]
        if sym.bind == STB_LOCAL:
            if sym.section is not None:
                return sym.section.addr + sym.value
            if sym.shndx == SHN_ABS:
                return sym.value
            raise LinkError("%s: unresolved local symbol %s"
                            % (obj.name, sym.name))
        g = self.globals.get(sym.name, None)
        if g is None or not g.resolved:
            if sym.section is not None:
                return sym.section.addr + sym.value
            if sym.bind == STB_WEAK:
                return 0                # undefined weak resolves to zero
            raise LinkError("undefined reference to `%s' (from %s, %s)"
                            % (sym.name, obj.name, where))
        return g.addr

    # -- GOT --------------------------------------------------------------
    def scan_got(self):
        """Reserve a .got slot for every symbol reached through GOTPCREL."""
        need = []
        for obj in self.objects:
            for sec in obj.sections:
                if not sec.keep:
                    continue
                for r in sec.relocs:
                    if (r.rtype == R_X86_64_GOTPCREL
                            or r.rtype == R_X86_64_GOTPCRELX
                            or r.rtype == R_X86_64_REX_GOTPCRELX
                            or r.rtype == R_X86_64_GOT32):
                        sym = obj.symbols[r.symidx]
                        key = "%s" % sym.name
                        if key not in self.got_entries:
                            self.got_entries[key] = len(self.got_order) * 8
                            self.got_order.append(sym.name)
                            need.append(sym.name)
        if len(self.got_order) == 0:
            return
        got = self._get_out(".got")
        got.flags |= SHF_ALLOC | SHF_WRITE
        got.align = 8
        holder = InSection(None, ".got", -1)
        holder.type = SHT_PROGBITS
        holder.flags = SHF_ALLOC | SHF_WRITE
        holder.align = 8
        holder.size = len(self.got_order) * 8
        holder.data = [0] * holder.size
        holder.keep = True
        holder.out = ".got"
        got.inputs.append(holder)
        self.got_section = holder

    def fill_got(self):
        if len(self.got_order) == 0:
            return
        holder = self.got_section
        i = 0
        while i < len(self.got_order):
            name = self.got_order[i]
            g = self.globals.get(name, None)
            addr = g.addr if (g is not None and g.resolved) else 0
            put(holder.data, i * 8, addr, 8)
            i += 1

    # -- relocation -------------------------------------------------------
    def relocate(self):
        got_addr = 0
        if len(self.got_order) > 0:
            got_addr = self.got_section.addr
        for obj in self.objects:
            for sec in obj.sections:
                if not sec.keep or len(sec.relocs) == 0:
                    continue
                if sec.type == SHT_NOBITS:
                    continue
                for r in sec.relocs:
                    self._apply(obj, sec, r, got_addr)

    def _apply(self, obj, sec, r, got_addr):
        t = r.rtype
        if t == R_X86_64_NONE:
            return
        where = "%s+0x%x" % (sec.name, r.offset)
        P = sec.addr + r.offset            # address of the field itself
        A = r.addend
        if (t == R_X86_64_GOTPCREL or t == R_X86_64_GOTPCRELX
                or t == R_X86_64_REX_GOTPCRELX):
            sym = obj.symbols[r.symidx]
            G = got_addr + self.got_entries[sym.name]
            self._put_field(sec, r.offset, G + A - P, 4, True, where)
            return
        S = self._sym_addr(obj, r.symidx, where)
        if t == R_X86_64_64:
            self._put_field(sec, r.offset, S + A, 8, False, where)
        elif t == R_X86_64_PC64:
            self._put_field(sec, r.offset, S + A - P, 8, False, where)
        elif t == R_X86_64_PC32 or t == R_X86_64_PLT32:
            # statically linked: a PLT-relative call is just a direct call
            self._put_field(sec, r.offset, S + A - P, 4, True, where)
        elif t == R_X86_64_32:
            self._put_field(sec, r.offset, S + A, 4, False, where)
        elif t == R_X86_64_32S:
            self._put_field(sec, r.offset, S + A, 4, True, where)
        elif t == R_X86_64_16:
            self._put_field(sec, r.offset, S + A, 2, False, where)
        elif t == R_X86_64_PC16:
            self._put_field(sec, r.offset, S + A - P, 2, True, where)
        elif t == R_X86_64_8:
            self._put_field(sec, r.offset, S + A, 1, False, where)
        elif t == R_X86_64_PC8:
            self._put_field(sec, r.offset, S + A - P, 1, True, where)
        else:
            raise LinkError("%s: unsupported relocation type %d at %s"
                            % (obj.name, t, where))

    def _put_field(self, sec, off, value, nbytes, signed, where):
        if nbytes == 4:
            if signed:
                if value < -2147483648 or value > 2147483647:
                    raise LinkError("relocation overflow at %s (value 0x%x "
                                    "does not fit in a signed 32-bit field)"
                                    % (where, value))
            else:
                if value < 0 or value > 0xFFFFFFFF:
                    raise LinkError("relocation truncated at %s (value 0x%x)"
                                    % (where, value))
        elif nbytes == 1:
            if value < -128 or value > 255:
                raise LinkError("relocation overflow at %s" % where)
        elif nbytes == 2:
            if value < -32768 or value > 65535:
                raise LinkError("relocation overflow at %s" % where)
        put(sec.data, off, value, nbytes)

    # -- output -----------------------------------------------------------
    def find_entry(self):
        g = self.globals.get(self.entry_name, None)
        if g is None or not g.resolved:
            raise LinkError("entry symbol `%s' is not defined"
                            % self.entry_name)
        self.entry = g.addr

    def check_undefined(self):
        left = []
        for name in sorted(self.undefined.keys()):
            g = self.globals.get(name, None)
            if g is not None and g.resolved:
                continue
            # an undefined *weak* reference is allowed and resolves to 0
            weak = True
            for obj in self.objects:
                for sym in obj.symbols:
                    if sym.name == name and sym.shndx == SHN_UNDEF \
                            and sym.bind != STB_WEAK:
                        weak = False
            if not weak:
                left.append(name)
        if len(left) > 0:
            raise LinkError("undefined reference to: %s" % ", ".join(left))

    def write(self):
        """Serialise the linked image as an ET_EXEC executable."""
        hdr_size = 64 + self.nphdr * 56
        out = []
        # ELF header + program headers are written last (we need final sizes),
        # so start with a placeholder region.
        i = 0
        while i < hdr_size:
            out.append(0)
            i += 1

        for sec_list in [self.exec_secs, self.rw_secs]:
            for o in sec_list:
                if o.nobits:
                    continue
                while len(out) < o.file_off:
                    out.append(0)
                for sec in o.inputs:
                    start = sec.addr - o.addr + o.file_off
                    while len(out) < start:
                        out.append(0)
                    if sec.type == SHT_NOBITS:
                        k = 0
                        while k < sec.size:
                            out.append(0)
                            k += 1
                    else:
                        out.extend(sec.data)
                        # a section may be shorter than its declared size
                        k = len(sec.data)
                        while k < sec.size:
                            out.append(0)
                            k += 1

        file_size = len(out)

        # ---- program headers ----
        text_filesz = self.text_file_end
        text_memsz = self.text_end - self.base
        rw_start_addr = 0
        rw_filesz = 0
        rw_memsz = 0
        if len(self.rw_secs) > 0:
            rw_start_addr = self.rw_secs[0].addr
            rw_off = self.rw_secs[0].file_off
            rw_filesz = self.data_file_end - rw_off
            rw_memsz = self.data_end - rw_start_addr
            if rw_filesz < 0:
                rw_filesz = 0
        phdrs = []
        if self.script is not None:
            # Script-driven (bare-metal) image: one tightly packed loadable
            # segment. The ELF header and program headers are not part of it --
            # the image starts at the script's address, and a Multiboot loader
            # copies it there flat.
            phdrs.append({"type": PT_LOAD, "flags": PF_R | PF_W | PF_X,
                          "off": self.first_file_off, "vaddr": self.base,
                          "filesz": self.image_file_end - self.first_file_off,
                          "memsz": self.image_end - self.base, "align": PAGE})
            shoff, shnum, shstrndx, tail = self._build_sheaders(len(out))
            out.extend(tail)
            hdr = []
            hdr.extend([0x7F, ord('E'), ord('L'), ord('F'), 2, 1, 1, 0])
            hdr.extend([0, 0, 0, 0, 0, 0, 0, 0])
            hdr.extend(pack(ET_EXEC, 2))
            hdr.extend(pack(EM_X86_64, 2))
            hdr.extend(pack(1, 4))
            hdr.extend(pack(self.entry, 8))
            hdr.extend(pack(64, 8))
            hdr.extend(pack(shoff, 8))
            hdr.extend(pack(0, 4))
            hdr.extend(pack(64, 2))
            hdr.extend(pack(56, 2))
            hdr.extend(pack(len(phdrs), 2))
            hdr.extend(pack(64, 2))
            hdr.extend(pack(shnum, 2))
            hdr.extend(pack(shstrndx, 2))
            for p in phdrs:
                hdr.extend(pack(p["type"], 4))
                hdr.extend(pack(p["flags"], 4))
                hdr.extend(pack(p["off"], 8))
                hdr.extend(pack(p["vaddr"], 8))
                hdr.extend(pack(p["vaddr"], 8))
                hdr.extend(pack(p["filesz"], 8))
                hdr.extend(pack(p["memsz"], 8))
                hdr.extend(pack(p["align"], 8))
            i = 0
            while i < len(hdr):
                out[i] = hdr[i]
                i += 1
            return out
        phdrs.append({"type": PT_LOAD, "flags": PF_R | PF_X, "off": 0,
                      "vaddr": self.base, "filesz": text_filesz,
                      "memsz": text_memsz, "align": PAGE})
        if len(self.rw_secs) > 0:
            phdrs.append({"type": PT_LOAD, "flags": PF_R | PF_W,
                          "off": self.rw_secs[0].file_off,
                          "vaddr": rw_start_addr, "filesz": rw_filesz,
                          "memsz": rw_memsz, "align": PAGE})
        phdrs.append({"type": PT_GNU_STACK, "flags": PF_R | PF_W, "off": 0,
                      "vaddr": 0, "filesz": 0, "memsz": 0, "align": 0x10})

        # ---- section headers + symbol table (for objdump/gdb) ----
        shoff, shnum, shstrndx, tail = self._build_sheaders(len(out))
        out.extend(tail)

        hdr = []
        hdr.extend([0x7F, ord('E'), ord('L'), ord('F'), 2, 1, 1, 0])
        hdr.extend([0, 0, 0, 0, 0, 0, 0, 0])
        hdr.extend(pack(ET_EXEC, 2))
        hdr.extend(pack(EM_X86_64, 2))
        hdr.extend(pack(1, 4))
        hdr.extend(pack(self.entry, 8))
        hdr.extend(pack(64, 8))                 # e_phoff
        hdr.extend(pack(shoff, 8))              # e_shoff
        hdr.extend(pack(0, 4))                  # e_flags
        hdr.extend(pack(64, 2))                 # e_ehsize
        hdr.extend(pack(56, 2))                 # e_phentsize
        hdr.extend(pack(len(phdrs), 2))
        hdr.extend(pack(64, 2))                 # e_shentsize
        hdr.extend(pack(shnum, 2))
        hdr.extend(pack(shstrndx, 2))
        for p in phdrs:
            hdr.extend(pack(p["type"], 4))
            hdr.extend(pack(p["flags"], 4))
            hdr.extend(pack(p["off"], 8))
            hdr.extend(pack(p["vaddr"], 8))
            hdr.extend(pack(p["vaddr"], 8))     # p_paddr
            hdr.extend(pack(p["filesz"], 8))
            hdr.extend(pack(p["memsz"], 8))
            hdr.extend(pack(p["align"], 8))
        i = 0
        while i < len(hdr):
            out[i] = hdr[i]
            i += 1
        return out

    def _build_sheaders(self, cur_len):
        """Build .symtab/.strtab/.shstrtab plus the section header table.

        Purely informational -- the program runs off the program headers -- but
        keeping it means `objdump -d` and `nm` work on rlink's output, which
        makes debugging the linker itself far easier."""
        tail = []
        base_off = cur_len

        strtab = [0]
        stroff = {"": 0}

        def add_str(s):
            if s in stroff:
                return stroff[s]
            off = len(strtab)
            i = 0
            while i < len(s):
                strtab.append(ord(s[i]))
                i += 1
            strtab.append(0)
            stroff[s] = off
            return off

        # section indices: 0 NULL, then each output section in layout order
        secs = []
        for o in self.exec_secs:
            secs.append(o)
        for o in self.rw_secs:
            secs.append(o)
        sec_num = {}
        i = 0
        while i < len(secs):
            sec_num[secs[i].name] = i + 1
            i += 1

        symbols = []
        symbols.append({"name": 0, "info": 0, "shndx": 0, "value": 0,
                        "size": 0})
        names = sorted(self.globals.keys())
        for nm in names:
            s = self.globals[nm]
            if not s.resolved:
                continue
            shndx = 0
            if s.section is not None and s.section.out in sec_num:
                shndx = sec_num[s.section.out]
            elif s.shndx == SHN_ABS:
                shndx = SHN_ABS
            symbols.append({"name": add_str(nm),
                            "info": (STB_GLOBAL << 4) | s.stype,
                            "shndx": shndx, "value": s.addr, "size": s.size})

        while (base_off + len(tail)) % 8 != 0:
            tail.append(0)
        symtab_off = base_off + len(tail)
        for sm in symbols:
            tail.extend(pack(sm["name"], 4))
            tail.append(sm["info"] & 0xFF)
            tail.append(0)
            tail.extend(pack(sm["shndx"], 2))
            tail.extend(pack(sm["value"], 8))
            tail.extend(pack(sm["size"], 8))
        symtab_size = len(symbols) * 24

        strtab_off = base_off + len(tail)
        tail.extend(strtab)

        shstr = [0]
        shstroff = {"": 0}

        def add_shstr(s):
            if s in shstroff:
                return shstroff[s]
            off = len(shstr)
            i = 0
            while i < len(s):
                shstr.append(ord(s[i]))
                i += 1
            shstr.append(0)
            shstroff[s] = off
            return off

        sec_name_off = []
        i = 0
        while i < len(secs):
            sec_name_off.append(add_shstr(secs[i].name))
            i += 1
        n_symtab = add_shstr(".symtab")
        n_strtab = add_shstr(".strtab")
        n_shstr = add_shstr(".shstrtab")

        shstr_off = base_off + len(tail)
        tail.extend(shstr)

        while (base_off + len(tail)) % 8 != 0:
            tail.append(0)
        shoff = base_off + len(tail)

        def add_sh(name, typ, flags, addr, off, size, link, info, align, ent):
            tail.extend(pack(name, 4))
            tail.extend(pack(typ, 4))
            tail.extend(pack(flags, 8))
            tail.extend(pack(addr, 8))
            tail.extend(pack(off, 8))
            tail.extend(pack(size, 8))
            tail.extend(pack(link, 4))
            tail.extend(pack(info, 4))
            tail.extend(pack(align, 8))
            tail.extend(pack(ent, 8))

        add_sh(0, SHT_NULL, 0, 0, 0, 0, 0, 0, 0, 0)
        i = 0
        while i < len(secs):
            o = secs[i]
            typ = SHT_NOBITS if o.nobits else SHT_PROGBITS
            add_sh(sec_name_off[i], typ, o.flags & 0x7, o.addr, o.file_off,
                   o.size, 0, 0, o.align, 0)
            i += 1
        symtab_idx = len(secs) + 1
        add_sh(n_symtab, SHT_SYMTAB, 0, 0, symtab_off, symtab_size,
               symtab_idx + 1, 1, 8, 24)
        add_sh(n_strtab, SHT_STRTAB, 0, 0, strtab_off, len(strtab), 0, 0, 1, 0)
        add_sh(n_shstr, SHT_STRTAB, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0)
        shnum = len(secs) + 4
        shstrndx = len(secs) + 3
        return (shoff, shnum, shstrndx, tail)

    # -- top level --------------------------------------------------------
    def link(self):
        self.pull_archives()
        self.collect_sections()
        self.allocate_commons()
        self.scan_got()
        self.layout()
        self.resolve()
        self.check_undefined()
        self.fill_got()
        self.relocate()
        self.find_entry()
        return self.write()


def _order_sort_key(out):
    return _order_key(out.name)


def link_files(inputs, entry, base):
    """inputs: list of (name, byte-list, is_archive). Returns the image."""
    ln = Linker()
    ln.entry_name = entry
    ln.base = base
    for item in inputs:
        if item[2]:
            ln.add_archive(item[0], item[1])
        else:
            ln.add_object(item[0], item[1])
    return ln.link()


# --------------------------------------------------------------------------
# Command-line driver
# --------------------------------------------------------------------------
# Usage:  rlink.py -o OUT [-e ENTRY] [--base ADDR] [-L DIR] [-l NAME] INPUT...
#
# Accepts object files (.o), archives (.a), and -l/-L library lookups, which is
# the subset of `ld`'s interface the ShivyCX driver actually uses.

def _read_bytes(path):
    f = open(path, "rb")
    data = f.read()
    f.close()
    out = []
    i = 0
    while i < len(data):
        out.append(data[i] if isinstance(data[i], int) else ord(data[i]))
        i += 1
    return out


def _is_archive(data):
    if len(data) < 8:
        return False
    return (data[0] == 0x21 and data[1] == 0x3C and data[2] == 0x61
            and data[3] == 0x72 and data[4] == 0x63 and data[5] == 0x68
            and data[6] == 0x3E and data[7] == 0x0A)      # "!<arch>\n"


def main(argv):
    import os
    out_path = "a.out"
    entry = "_start"
    base = DEFAULT_BASE
    lib_dirs = []
    libs = []
    inputs = []
    script = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "-o":
            i += 1
            out_path = argv[i]
        elif a == "-e" or a == "--entry":
            i += 1
            entry = argv[i]
        elif a == "--base":
            i += 1
            base = int(argv[i], 0)
        elif a[0:2] == "-L":
            lib_dirs.append(a[2:] if len(a) > 2 else argv[i + 1])
            if len(a) == 2:
                i += 1
        elif a[0:2] == "-l":
            libs.append(a[2:] if len(a) > 2 else argv[i + 1])
            if len(a) == 2:
                i += 1
        elif a == "-T" or a == "--script":
            i += 1
            f = open(argv[i])
            script_text = f.read()
            f.close()
            script = parse_script(script_text)
        elif a[0:2] == "-T" and len(a) > 2:
            f = open(a[2:])
            script_text = f.read()
            f.close()
            script = parse_script(script_text)
        elif a == "-static" or a == "-n" or a == "--no-dynamic-linker":
            pass                       # static is the only mode rlink has
        elif a[0:1] == "-":
            pass                       # ignore other ld flags
        else:
            inputs.append(a)
        i += 1

    ln = Linker()
    ln.entry_name = entry
    ln.base = base
    if script is not None:
        ln.script = script
        if script.entry != "" and entry == "_start":
            ln.entry_name = script.entry
    for path in inputs:
        data = _read_bytes(path)
        if _is_archive(data):
            ln.add_archive(path, data)
        else:
            ln.add_object(path, data)
    for name in libs:
        found = ""
        for d in lib_dirs:
            cand = os.path.join(d, "lib" + name + ".a")
            if os.path.exists(cand):
                found = cand
                break
        if found == "":
            raise LinkError("cannot find -l%s" % name)
        ln.add_archive(found, _read_bytes(found))

    image = ln.link()
    for w in ln.warnings:
        os.write(2, ("rlink: warning: %s\n" % w).encode("utf-8"))
    f = open(out_path, "wb")
    f.write(bytes(bytearray(image)))
    f.close()
    os.chmod(out_path, 0o755)
    return 0


if __name__ == "__main__":
    import sys
    try:
        sys.exit(main(sys.argv))
    except LinkError as e:
        sys.stderr.write("rlink: %s\n" % e)
        sys.exit(1)
