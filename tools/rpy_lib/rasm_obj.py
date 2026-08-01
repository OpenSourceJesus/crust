"""rasm_obj -- assembler driver + ELF64 object writer.

Turns a full Intel- or AT&T-syntax assembly file into an ELF64 relocatable
object, so it can replace `as -o x.o x.s`.

The driver builds each section as a list of *fragments* rather than a flat byte
list, because two things make a section's layout non-obvious in a single pass:

  * **branch relaxation** -- a jump whose target is a nearby local label can use
    the two-byte rel8 form instead of the five/six-byte rel32 form, but whether
    it reaches depends on the sizes of the branches between here and there;
  * **alignment** -- `.align n` emits a variable amount of padding that depends
    on the current offset, which in turn depends on the branches before it.

So layout is a fixpoint: every relaxable branch starts short, the section is
laid out, and any branch that turns out not to reach is promoted to the long
form. Promotion is one-way, so the loop terminates; when a pass promotes
nothing, the layout is stable and fragments are flattened into bytes.

Kept flat and RPython-friendly (uniform record classes, explicit byte lists).
"""
import rasm


# --------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------
FRAG_BYTES = 0     # fixed bytes (an encoded instruction, or data)
FRAG_BRANCH = 1    # a jmp/jcc whose encoding may shrink
FRAG_ALIGN = 2     # padding to a power-of-two boundary


class Frag(object):
    def __init__(self, kind):
        self.kind = kind
        self.data = []          # FRAG_BYTES: the encoded bytes
        self.relocs = []        # rasm.Reloc, .where relative to frag start
        # FRAG_BRANCH
        self.bkind = ""         # "jmp" | "jcc"
        self.tttn = 0
        self.sym = ""
        self.add = 0            # extra addend from the operand
        self.short = False
        # FRAG_ALIGN
        self.align = 1
        self.fill = 0
        # computed by layout()
        self.offset = 0
        self.size = 0


def _branch_long_size(bkind):
    if bkind == "jcc":
        return 6            # 0F 8x rel32
    return 5                # E9 rel32


class Section(object):
    def __init__(self, name):
        self.name = name
        self.frags = []
        self.labels = []        # (label name, fragment index)
        self.data = []          # flattened bytes (filled by finalize)
        self.relocs = []        # flattened relocs (filled by finalize)
        self.align = 1
        self.nobits = (name == ".bss" or name[0:5] == ".bss.")
        self.size = 0

    # -- construction -----------------------------------------------------
    def _bytes_frag(self):
        if len(self.frags) > 0 and self.frags[-1].kind == FRAG_BYTES:
            return self.frags[-1]
        f = Frag(FRAG_BYTES)
        self.frags.append(f)
        return f

    def emit(self, byte_list):
        self._bytes_frag().data.extend(byte_list)

    def emit_with_relocs(self, byte_list, relocs):
        f = self._bytes_frag()
        base = len(f.data)
        f.data.extend(byte_list)
        i = 0
        while i < len(relocs):
            r = relocs[i]
            f.relocs.append(rasm.Reloc(base + r.where, r.sym, r.size,
                                       r.pcrel, r.add))
            i += 1

    def emit_branch(self, bkind, tttn, sym, add):
        f = Frag(FRAG_BRANCH)
        f.bkind = bkind
        f.tttn = tttn
        f.sym = sym
        f.add = add
        f.short = True          # optimistic; layout may promote it
        self.frags.append(f)

    def emit_align(self, n, fill):
        f = Frag(FRAG_ALIGN)
        f.align = n
        f.fill = fill
        self.frags.append(f)
        if n > self.align:
            self.align = n

    def mark_label(self, name):
        # Open a fresh (empty) bytes fragment so the label binds to exactly
        # this point. Without this, bytes emitted after the label would be
        # appended to the *preceding* fragment and the label would resolve to
        # that fragment's start -- collapsing every label in a run onto the
        # same offset.
        f = Frag(FRAG_BYTES)
        self.frags.append(f)
        self.labels.append((name, len(self.frags) - 1))


class AsmError(Exception):
    pass


class Symbol(object):
    def __init__(self, name):
        self.name = name
        self.section = ""     # section where defined ("" if undefined)
        self.value = 0        # offset within its section
        self.is_global = False
        self.defined = False
        self.size = 0
        self.common = False   # SHN_COMMON (from .comm)
        self.is_func = False


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
_DATA_WIDTH = {".byte": 1, ".word": 2, ".short": 2, ".value": 2,
               ".int": 4, ".long": 4, ".quad": 8}


def _unescape(s):
    """Decode a .string/.ascii operand's C-style escapes into byte values."""
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            i += 2
            if n == "n":
                out.append(10)
            elif n == "t":
                out.append(9)
            elif n == "r":
                out.append(13)
            elif n == "0":
                out.append(0)
            elif n == "\\":
                out.append(92)
            elif n == "\"":
                out.append(34)
            elif n == "a":
                out.append(7)
            elif n == "b":
                out.append(8)
            elif n == "f":
                out.append(12)
            elif n == "v":
                out.append(11)
            elif n == "x":
                h = ""
                while i < len(s) and len(h) < 2:
                    ch = s[i]
                    if (("0" <= ch <= "9") or ("a" <= ch <= "f")
                            or ("A" <= ch <= "F")):
                        h += ch
                        i += 1
                    else:
                        break
                out.append(int(h, 16) & 0xFF)
            else:
                out.append(ord(n))
        else:
            out.append(ord(c))
            i += 1
    return out


def _split_quoted(rest):
    """Return the contents of the first double-quoted string in `rest`."""
    a = rest.find("\"")
    if a < 0:
        return ""
    i = a + 1
    out = ""
    while i < len(rest):
        c = rest[i]
        if c == "\\":
            out += rest[i:i + 2]
            i += 2
            continue
        if c == "\"":
            break
        out += c
        i += 1
    return out


class Assembler(object):
    def __init__(self):
        self.sections = {}
        self.order = []
        self.symbols = {}
        self.att_mode = False
        self._get_section(".text")
        self._get_section(".data")
        self._get_section(".bss")
        self.cur = self.sections[".text"]

    def _get_section(self, name):
        if name not in self.sections:
            s = Section(name)
            self.sections[name] = s
            self.order.append(name)
        return self.sections[name]

    def _sym(self, name):
        if name not in self.symbols:
            self.symbols[name] = Symbol(name)
        return self.symbols[name]

    # -- entry point ------------------------------------------------------
    def assemble(self, text):
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            self._line(lines[i])
            i += 1
        self.layout()
        self.finalize()

    def _line(self, raw):
        if self.att_mode:
            triple = rasm.parse_att_line(raw)
        else:
            triple = rasm.parse_line(raw)
        kind = triple[0]
        a = triple[1]
        ops = triple[2]
        if kind == "blank":
            return
        if kind == "label":
            sym = self._sym(a)
            sym.section = self.cur.name
            sym.defined = True
            self.cur.mark_label(a)
            return
        if kind == "dir":
            self._directive(a)
            return
        # instruction: relaxable branches become their own fragment
        bk = rasm.branch_kind(a, ops)
        if bk[0] == "jmp" or bk[0] == "jcc":
            self.cur.emit_branch(bk[0], bk[1], ops[0].sym, ops[0].imm)
            return
        body, relocs = rasm.encode(a, ops)
        self.cur.emit_with_relocs(body, relocs)

    # -- directives -------------------------------------------------------
    def _directive(self, line):
        parts = line.split()
        d = parts[0]
        if d == ".intel_syntax":
            self.att_mode = False
            return
        if d == ".att_syntax":
            self.att_mode = True
            return
        if d == ".section":
            name = parts[1] if len(parts) > 1 else ".text"
            comma = name.find(",")
            if comma >= 0:
                name = name[:comma]
            self.cur = self._get_section(name)
            return
        if d == ".text" or d == ".data" or d == ".bss" or d == ".rodata":
            self.cur = self._get_section(d)
            return
        if d == ".global" or d == ".globl":
            self._sym(parts[1].strip(",")).is_global = True
            return
        if d == ".type":
            rest = line[len(d):].strip()
            comma = rest.find(",")
            if comma > 0:
                nm = rest[:comma].strip()
                what = rest[comma + 1:].strip()
                if what == "@function" or what == "%function":
                    self._sym(nm).is_func = True
            return
        if d == ".size":
            rest = line[len(d):].strip()
            comma = rest.find(",")
            if comma > 0:
                nm = rest[:comma].strip()
                val = rest[comma + 1:].strip()
                if rasm._looks_int(val):
                    self._sym(nm).size = rasm._parse_int(val)
            return
        if d == ".comm" or d == ".lcomm":
            nm = parts[1].strip(",")
            sym = self._sym(nm)
            sym.size = int(parts[2].strip(","))
            sym.common = True
            sym.is_global = (d == ".comm")
            sym.defined = True
            return
        if d == ".zero" or d == ".skip" or d == ".space":
            n = int(parts[1].strip(","))
            self.cur.emit([0] * n)
            return
        if d in _DATA_WIDTH:
            self._data(d, line[len(d):].strip())
            return
        if d == ".align" or d == ".balign":
            n = int(parts[1].strip(",")) if len(parts) > 1 else 1
            self._align(n)
            return
        if d == ".p2align":
            p = int(parts[1].strip(",")) if len(parts) > 1 else 0
            self._align(1 << p)
            return
        if d == ".string" or d == ".asciz" or d == ".ascii":
            bs = _unescape(_split_quoted(line))
            if d != ".ascii":
                bs.append(0)
            self.cur.emit(bs)
            return
        # unknown directive: ignore silently to stay robust
        return

    def _align(self, n):
        if n <= 1:
            return
        fill = 0x90 if self.cur.name[0:5] == ".text" else 0
        self.cur.emit_align(n, fill)

    def _data(self, d, rest):
        width = _DATA_WIDTH[d]
        items = rest.split(",")
        k = 0
        while k < len(items):
            v = items[k].strip()
            if v != "":
                if rasm._looks_int(v):
                    self.cur.emit(rasm.pack_le(rasm._parse_int(v), width))
                else:
                    # a symbol reference in data -> absolute relocation
                    sym = v
                    add = 0
                    plus = v.find("+")
                    if plus > 0:
                        sym = v[:plus]
                        add = rasm._parse_int(v[plus + 1:])
                    self.cur.emit_with_relocs(
                        rasm.pack_le(0, width),
                        [rasm.Reloc(0, sym, width, False, add)])
            k += 1

    # -- layout + relaxation ---------------------------------------------
    def _layout_once(self, sec):
        """Assign every fragment an offset given the current short/long flags,
        then bind this section's labels to the offsets they landed on."""
        off = 0
        i = 0
        while i < len(sec.frags):
            f = sec.frags[i]
            f.offset = off
            if f.kind == FRAG_BYTES:
                f.size = len(f.data)
            elif f.kind == FRAG_BRANCH:
                f.size = 2 if f.short else _branch_long_size(f.bkind)
            else:
                rem = off % f.align
                f.size = 0 if rem == 0 else (f.align - rem)
            off += f.size
            i += 1
        sec.size = off
        j = 0
        while j < len(sec.labels):
            pair = sec.labels[j]
            idx = pair[1]
            s = self.symbols[pair[0]]
            if idx < len(sec.frags):
                s.value = sec.frags[idx].offset
            else:
                s.value = off
            j += 1

    def _relaxable(self, sec, f):
        """A branch can use rel8 only if its target is a local label defined in
        this same section -- otherwise the distance is not known until link
        time, and there is no 8-bit relocation to defer it with."""
        s = self.symbols.get(f.sym, None)
        if s is None or not s.defined or s.common:
            return False
        if s.is_global:
            return False        # keep a relocation so the linker can preempt
        return s.section == sec.name

    def layout(self):
        for name in self.order:
            sec = self.sections[name]
            i = 0
            while i < len(sec.frags):
                f = sec.frags[i]
                if f.kind == FRAG_BRANCH and not self._relaxable(sec, f):
                    f.short = False
                i += 1
            rounds = 0
            while True:
                self._layout_once(sec)
                changed = False
                i = 0
                while i < len(sec.frags):
                    f = sec.frags[i]
                    if f.kind == FRAG_BRANCH and f.short:
                        target = self.symbols[f.sym].value
                        rel = target + f.add - (f.offset + f.size)
                        if not rasm.fits_int8(rel):
                            f.short = False
                            changed = True
                    i += 1
                rounds += 1
                if not changed:
                    break
                if rounds > 1000:       # promotion is one-way; belt and braces
                    self._layout_once(sec)
                    break

    # -- flatten ----------------------------------------------------------
    def finalize(self):
        for name in self.order:
            sec = self.sections[name]
            data = []
            relocs = []
            i = 0
            while i < len(sec.frags):
                f = sec.frags[i]
                if f.kind == FRAG_ALIGN:
                    k = 0
                    while k < f.size:
                        data.append(f.fill)
                        k += 1
                    i += 1
                    continue
                base = len(data)
                if f.kind == FRAG_BYTES:
                    body = f.data
                    rl = f.relocs
                else:
                    body, rl = self._encode_branch(f)
                data.extend(body)
                j = 0
                while j < len(rl):
                    r = rl[j]
                    relocs.append(rasm.Reloc(base + r.where, r.sym, r.size,
                                             r.pcrel, r.add))
                    j += 1
                i += 1
            sec.data = data
            sec.relocs = relocs
            if not sec.nobits:
                sec.size = len(data)
        self._resolve()

    def _encode_branch(self, f):
        o = rasm.Operand()
        o.kind = "imm"
        o.sym = f.sym
        o.imm = f.add
        o.disp = f.add
        if f.short:
            if f.bkind == "jmp":
                return rasm.encode_jmp_short(o)
            return rasm.encode_jcc_short(f.tttn, o)
        if f.bkind == "jcc":
            return rasm._encode_jcc(f.tttn, o)
        return rasm._encode_calljmp(o, False)

    def _resolve(self):
        """Resolve same-section PC-relative refs to local (non-global) labels
        in place; keep the rest as ELF relocations."""
        for name in self.order:
            sec = self.sections[name]
            keep = []
            j = 0
            while j < len(sec.relocs):
                r = sec.relocs[j]
                sym = self.symbols.get(r.sym, None)
                resolved = False
                if (r.pcrel and sym is not None and sym.defined
                        and not sym.common and not sym.is_global
                        and sym.section == sec.name):
                    # ELF PC-relative value: S + A - P (A already folded to -N)
                    rel = sym.value + r.add - r.where
                    patch = rasm.pack_le(rel, r.size)
                    m = 0
                    while m < r.size:
                        sec.data[r.where + m] = patch[m]
                        m += 1
                    resolved = True
                if not resolved:
                    if r.size == 1:
                        raise AsmError(
                            "8-bit branch to non-local symbol %s" % r.sym)
                    keep.append(r)
                j += 1
            sec.relocs = keep


# --------------------------------------------------------------------------
# ELF64 relocatable-object writer
# --------------------------------------------------------------------------
SHN_UNDEF = 0
SHN_COMMON = 0xFFF2

SHT_PROGBITS = 1
SHT_SYMTAB = 2
SHT_STRTAB = 3
SHT_RELA = 4
SHT_NOBITS = 8

SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4

STB_LOCAL = 0
STB_GLOBAL = 1
STT_NOTYPE = 0
STT_OBJECT = 1
STT_FUNC = 2
STT_SECTION = 3

R_X86_64_64 = 1
R_X86_64_PC32 = 2
R_X86_64_32 = 10
R_X86_64_32S = 11


def _u16(v):
    return rasm.pack_le(v, 2)


def _u32(v):
    return rasm.pack_le(v, 4)


def _u64(v):
    return rasm.pack_le(v, 8)


class _StrTab(object):
    def __init__(self):
        self.data = [0]        # index 0 is the empty string
        self.map = {"": 0}

    def add(self, s):
        if s in self.map:
            return self.map[s]
        off = len(self.data)
        i = 0
        while i < len(s):
            self.data.append(ord(s[i]))
            i += 1
        self.data.append(0)
        self.map[s] = off
        return off


def _reloc_type(r):
    if r.pcrel:
        return R_X86_64_PC32
    if r.size == 8:
        return R_X86_64_64
    return R_X86_64_32S


def _sec_flags(name):
    if name[0:5] == ".text":
        return SHF_ALLOC | SHF_EXECINSTR
    if name[0:7] == ".rodata":
        return SHF_ALLOC
    if name[0:5] == ".note":
        return 0
    return SHF_ALLOC | SHF_WRITE


def _sec_align(sec):
    if sec.align > 1:
        return sec.align
    if sec.name[0:5] == ".text":
        return 16
    return 8


def _emitted_sections(asm):
    """Sections that go into the object: always .text/.data/.bss, plus any
    other section that actually received content."""
    out = [".text", ".data", ".bss"]
    for name in asm.order:
        if name in out:
            continue
        if name[0:5] == ".note":
            continue
        sec = asm.sections[name]
        if len(sec.data) > 0 or len(sec.relocs) > 0 or sec.size > 0:
            out.append(name)
    return out


def write_elf(asm):
    data_secs = _emitted_sections(asm)

    sec_index = {}
    idx = 1
    for sn in data_secs:
        sec_index[sn] = idx
        idx += 1

    shstr = _StrTab()
    strtab = _StrTab()
    syms = []
    symindex = {}

    syms.append({"name": 0, "info": 0, "shndx": 0, "value": 0, "size": 0})

    secsym_index = {}
    for sn in data_secs:
        i = len(syms)
        secsym_index[sn] = i
        syms.append({"name": 0, "info": (STB_LOCAL << 4) | STT_SECTION,
                     "shndx": sec_index[sn], "value": 0, "size": 0})

    # Local symbols a relocation references (e.g. ShivyCX float literals
    # `__fltlitN` in .data) must precede the first global so sh_info is right.
    referenced = {}
    for sn in data_secs:
        sec = asm.sections[sn]
        for r in sec.relocs:
            referenced[r.sym] = True
    local_names = sorted(referenced.keys())
    for nm in local_names:
        s = asm.symbols.get(nm, None)
        if s is not None and s.defined and not s.is_global and not s.common:
            i = len(syms)
            symindex[nm] = i
            styp = STT_FUNC if (s.is_func or s.section[0:5] == ".text") \
                else STT_OBJECT
            syms.append({"name": strtab.add(nm),
                         "info": (STB_LOCAL << 4) | styp,
                         "shndx": sec_index.get(s.section, 0),
                         "value": s.value, "size": s.size})

    first_global = len(syms)

    names = sorted(asm.symbols.keys())
    for nm in names:
        s = asm.symbols[nm]
        if s.common:
            i = len(syms)
            symindex[nm] = i
            syms.append({"name": strtab.add(nm),
                         "info": (STB_GLOBAL << 4) | STT_OBJECT,
                         "shndx": SHN_COMMON, "value": 8, "size": s.size})
        elif s.is_global and s.defined:
            styp = STT_FUNC if (s.is_func or s.section[0:5] == ".text") \
                else STT_OBJECT
            i = len(syms)
            symindex[nm] = i
            syms.append({"name": strtab.add(nm),
                         "info": (STB_GLOBAL << 4) | styp,
                         "shndx": sec_index.get(s.section, 0),
                         "value": s.value, "size": s.size})
    for sn in data_secs:
        sec = asm.sections[sn]
        for r in sec.relocs:
            if r.sym not in symindex and r.sym not in secsym_index:
                i = len(syms)
                symindex[r.sym] = i
                syms.append({"name": strtab.add(r.sym),
                             "info": (STB_GLOBAL << 4) | STT_NOTYPE,
                             "shndx": SHN_UNDEF, "value": 0, "size": 0})

    # ---- file body ----
    ehdr_size = 64
    body = []

    sec_file = {}
    for sn in data_secs:
        sec = asm.sections[sn]
        if sec.nobits:
            sec_file[sn] = ehdr_size + len(body)
            continue
        a = _sec_align(sec)
        while (ehdr_size + len(body)) % a != 0:
            body.append(0)
        sec_file[sn] = ehdr_size + len(body)
        body.extend(sec.data)

    while (ehdr_size + len(body)) % 8 != 0:
        body.append(0)
    sym_file = ehdr_size + len(body)
    for sm in syms:
        body.extend(_u32(sm["name"]))
        body.append(sm["info"] & 0xFF)
        body.append(0)  # st_other
        body.extend(_u16(sm["shndx"]))
        body.extend(_u64(sm["value"]))
        body.extend(_u64(sm["size"]))
    sym_size = len(syms) * 24

    str_file = ehdr_size + len(body)
    body.extend(strtab.data)

    rela_files = {}
    rela_sizes = {}
    for sn in data_secs:
        sec = asm.sections[sn]
        if len(sec.relocs) == 0:
            continue
        while (ehdr_size + len(body)) % 8 != 0:
            body.append(0)
        rela_files[sn] = ehdr_size + len(body)
        for r in sec.relocs:
            si = symindex.get(r.sym, secsym_index.get(sn, 0))
            info = (si << 32) | _reloc_type(r)
            body.extend(_u64(r.where))
            body.extend(_u64(info))
            body.extend(rasm.pack_le(r.add, 8))
        rela_sizes[sn] = len(sec.relocs) * 24

    # ---- names (must be interned before shstrtab is written) ----
    name_off = {}
    for sn in data_secs:
        name_off[sn] = shstr.add(sn)
    n_symtab = shstr.add(".symtab")
    n_strtab = shstr.add(".strtab")
    n_shstr = shstr.add(".shstrtab")
    rela_name_off = {}
    for sn in data_secs:
        if sn in rela_files:
            rela_name_off[sn] = shstr.add(".rela" + sn)
    n_note = shstr.add(".note.GNU-stack")

    shstr_file = ehdr_size + len(body)
    body.extend(shstr.data)

    # ---- section headers ----
    sh = []

    def add_sh(name, typ, flags, off, size, link, info, align, entsize):
        sh.append({"name": name, "type": typ, "flags": flags, "addr": 0,
                   "off": off, "size": size, "link": link, "info": info,
                   "align": align, "entsize": entsize})

    add_sh(0, 0, 0, 0, 0, 0, 0, 0, 0)                       # 0 NULL
    for sn in data_secs:
        sec = asm.sections[sn]
        typ = SHT_NOBITS if sec.nobits else SHT_PROGBITS
        size = sec.size if sec.nobits else len(sec.data)
        add_sh(name_off[sn], typ, _sec_flags(sn), sec_file[sn], size,
               0, 0, _sec_align(sec), 0)
    symtab_idx = len(sh)
    add_sh(n_symtab, SHT_SYMTAB, 0, sym_file, sym_size,
           symtab_idx + 1, first_global, 8, 24)
    strtab_idx = symtab_idx + 1
    add_sh(n_strtab, SHT_STRTAB, 0, str_file, len(strtab.data), 0, 0, 1, 0)
    shstr_idx = strtab_idx + 1
    add_sh(n_shstr, SHT_STRTAB, 0, shstr_file, len(shstr.data), 0, 0, 1, 0)
    for sn in data_secs:
        if sn in rela_files:
            add_sh(rela_name_off[sn], SHT_RELA, 0, rela_files[sn],
                   rela_sizes[sn], symtab_idx, sec_index[sn], 8, 24)
    # empty .note.GNU-stack marks the stack non-executable (silences linker)
    add_sh(n_note, SHT_PROGBITS, 0, shstr_file, 0, 0, 0, 1, 0)

    while (ehdr_size + len(body)) % 8 != 0:
        body.append(0)
    shoff = ehdr_size + len(body)
    for h in sh:
        body.extend(_u32(h["name"]))
        body.extend(_u32(h["type"]))
        body.extend(_u64(h["flags"]))
        body.extend(_u64(h["addr"]))
        body.extend(_u64(h["off"]))
        body.extend(_u64(h["size"]))
        body.extend(_u32(h["link"]))
        body.extend(_u32(h["info"]))
        body.extend(_u64(h["align"]))
        body.extend(_u64(h["entsize"]))

    eh = []
    eh.extend([0x7F, ord('E'), ord('L'), ord('F'), 2, 1, 1, 0])
    eh.extend([0, 0, 0, 0, 0, 0, 0, 0])       # e_ident padding
    eh.extend(_u16(1))                         # e_type ET_REL
    eh.extend(_u16(62))                        # e_machine EM_X86_64
    eh.extend(_u32(1))                         # e_version
    eh.extend(_u64(0))                         # e_entry
    eh.extend(_u64(0))                         # e_phoff
    eh.extend(_u64(shoff))                     # e_shoff
    eh.extend(_u32(0))                         # e_flags
    eh.extend(_u16(64))                        # e_ehsize
    eh.extend(_u16(0))                         # e_phentsize
    eh.extend(_u16(0))                         # e_phnum
    eh.extend(_u16(64))                        # e_shentsize
    eh.extend(_u16(len(sh)))                   # e_shnum
    eh.extend(_u16(shstr_idx))                 # e_shstrndx

    out = []
    out.extend(eh)
    out.extend(body)
    return out


def assemble_to_elf(text):
    a = Assembler()
    a.assemble(text)
    return write_elf(a)
