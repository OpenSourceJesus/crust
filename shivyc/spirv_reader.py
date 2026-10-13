"""SPIR-V binary decoding, disassembly and validation -- the inverse of
shivyc/spirv.py.

    .c --> shivyc --> .spv            (shivyc/spirv.py, spirv_gen.py)
    .spv --> parse / disassemble / validate   (this module)

There is no spirv-val in the toolchain and there should not need to be one:
`validate()` checks the rules a driver would otherwise discover for us, so a
bug in the emitter is caught here, by us, with a message that names the
instruction. It is independent of the emitter -- it works from the bytes.

What is checked: header and id bound; the section order the spec requires;
single definition of every id; every id operand defined; definition dominates
use (phi operands against the incoming edge); function and block structure;
the structured-control-flow rules the Vulkan `Shader` capability imposes
(merge instructions, back edges, break/continue targets); no recursion;
capability and version consistency; and buffer layout (Block, Offset,
ArrayStride, Binding / DescriptorSet).

Two deliberate strictnesses, because this validates what our emitter
produces, not every legal module: a loop's continue construct is a single
block (the back-edge block *is* the continue target), and layouts are checked
against *scalar* alignment (VK_EXT_scalar_block_layout, core in Vulkan 1.2),
which is what the plan commits to so that C struct layout maps across.

Self-host note: plain loops and instance attributes only.
"""

import sys

import shivyc.spirv as sp


class Inst:
    """One decoded instruction."""

    def __init__(self, code, name, words):
        self.code = code
        self.name = name
        self.words = words          # whole instruction, header word first
        self.type_id = 0
        self.result_id = 0
        self.operands = []          # list of (kind, value); kind in I L S


class Func:
    def __init__(self, inst):
        self.inst = inst
        self.id = inst.result_id
        self.fn_type = inst.operands[1][1] if len(inst.operands) > 1 else 0
        self.params = []
        self.blocks = []            # list of Blk, in module order
        self.block_by_label = {}


class Blk:
    def __init__(self, label_inst):
        self.label = label_inst.result_id
        self.insts = []             # everything after OpLabel, terminator last

    def terminator(self):
        if self.insts:
            return self.insts[-1]
        return None

    def successors(self):
        t = self.terminator()
        if t is None:
            return []
        if t.name == "OpBranch":
            return [t.operands[0][1]]
        if t.name == "OpBranchConditional":
            return [t.operands[1][1], t.operands[2][1]]
        if t.name == "OpSwitch":
            out = [t.operands[1][1]]
            i = 2
            while i < len(t.operands):
                if t.operands[i][0] == "I":
                    out.append(t.operands[i][1])
                i = i + 1
            return out
        return []


class Parsed:
    def __init__(self):
        self.version = 0
        self.bound = 0
        self.insts = []
        self.defs = {}              # id -> Inst, for ids outside functions
        self.names = {}
        self.member_names = {}
        self.decorations = {}       # id -> [(decoration, [args])]
        self.member_decorations = {}  # (id, member) -> [(decoration, [args])]
        self.entry_points = []      # (model, func id, name, interface ids)
        self.exec_modes = {}        # func id -> {mode: [args]}
        self.ext_imports = {}       # id -> name
        self.capabilities = []
        self.memory_model = None    # (addressing, model)
        self.global_insts = []      # SEC_GLOBALS instructions in order
        self.functions = []
        self.func_by_id = {}
        self.fn_def = {}            # function result id -> Func


def parse_operands(sig, words, start):
    """Decode the operand words of one instruction by its signature."""
    out = []
    i = start
    n = len(words)
    p = 0
    while p < len(sig):
        c = sig[p]
        repeat = (p + 1 < len(sig) and sig[p + 1] == "*")
        while True:
            if i >= n:
                if repeat:
                    break
                raise ValueError("instruction truncated: missing operand")
            if c == "I":
                out.append(("I", words[i]))
                i = i + 1
            elif c == "L":
                out.append(("L", words[i]))
                i = i + 1
            elif c == "S":
                s, i = sp.decode_string(words, i)
                out.append(("S", s))
            elif c == "P":
                if i + 1 >= n:
                    raise ValueError("instruction truncated: odd pair")
                out.append(("L", words[i]))
                out.append(("I", words[i + 1]))
                i = i + 2
            elif c == "Q":
                if i + 1 >= n:
                    raise ValueError("instruction truncated: odd pair")
                out.append(("I", words[i]))
                out.append(("I", words[i + 1]))
                i = i + 2
            else:
                raise ValueError("bad signature char " + c)
            if not repeat:
                break
        p = p + (2 if repeat else 1)
    if i != n:
        raise ValueError("instruction has %d trailing operand words" % (n - i))
    return out


def decode(words):
    """Words -> (header, [Inst]). Raises ValueError on a malformed stream."""
    if len(words) < 5:
        raise ValueError("module shorter than the 5-word header")
    if words[0] != sp.MAGIC:
        raise ValueError("bad magic number 0x%08x" % words[0])
    insts = []
    i = 5
    while i < len(words):
        w = words[i]
        wc = w >> 16
        code = w & 0xFFFF
        if wc == 0:
            raise ValueError("word %d: instruction with word count 0" % i)
        if i + wc > len(words):
            raise ValueError("word %d: instruction runs past the end" % i)
        name = sp.OPCODE_BY_CODE.get(code)
        if name is None:
            raise ValueError("word %d: opcode %d is outside the supported "
                             "subset" % (i, code))
        entry = sp.OPCODES[name]
        ins = Inst(code, name, words[i:i + wc])
        j = i + 1
        if entry[1]:
            if j >= i + wc:
                raise ValueError("%s: missing result type" % name)
            ins.type_id = words[j]
            j = j + 1
        if entry[2]:
            if j >= i + wc:
                raise ValueError("%s: missing result id" % name)
            ins.result_id = words[j]
            j = j + 1
        ins.operands = parse_operands(entry[3], words[:i + wc], j)
        insts.append(ins)
        i = i + wc
    return words[:5], insts


def parse(words):
    """Decode and organise a module. Raises ValueError on structural errors."""
    header, insts = decode(words)
    m = Parsed()
    m.version = header[1]
    m.bound = header[3]
    m.insts = insts
    cur = None
    blk = None
    for ins in insts:
        n = ins.name
        if cur is None:
            if n == "OpCapability":
                m.capabilities.append(ins.operands[0][1])
            elif n == "OpMemoryModel":
                m.memory_model = (ins.operands[0][1], ins.operands[1][1])
            elif n == "OpExtInstImport":
                m.ext_imports[ins.result_id] = ins.operands[0][1]
                m.defs[ins.result_id] = ins
            elif n == "OpEntryPoint":
                iface = []
                k = 3
                while k < len(ins.operands):
                    iface.append(ins.operands[k][1])
                    k = k + 1
                m.entry_points.append((ins.operands[0][1], ins.operands[1][1],
                                       ins.operands[2][1], iface))
            elif n == "OpExecutionMode":
                fid = ins.operands[0][1]
                if fid not in m.exec_modes:
                    m.exec_modes[fid] = {}
                args = []
                k = 2
                while k < len(ins.operands):
                    args.append(ins.operands[k][1])
                    k = k + 1
                m.exec_modes[fid][ins.operands[1][1]] = args
            elif n == "OpName":
                m.names[ins.operands[0][1]] = ins.operands[1][1]
            elif n == "OpMemberName":
                m.member_names[(ins.operands[0][1], ins.operands[1][1])] = \
                    ins.operands[2][1]
            elif n == "OpDecorate":
                tid = ins.operands[0][1]
                args = []
                k = 2
                while k < len(ins.operands):
                    args.append(ins.operands[k][1])
                    k = k + 1
                if tid not in m.decorations:
                    m.decorations[tid] = []
                m.decorations[tid].append((ins.operands[1][1], args))
            elif n == "OpMemberDecorate":
                key = (ins.operands[0][1], ins.operands[1][1])
                args = []
                k = 3
                while k < len(ins.operands):
                    args.append(ins.operands[k][1])
                    k = k + 1
                if key not in m.member_decorations:
                    m.member_decorations[key] = []
                m.member_decorations[key].append((ins.operands[2][1], args))
            elif n == "OpSource":
                pass
            elif n == "OpFunction":
                cur = Func(ins)
                m.functions.append(cur)
                m.func_by_id[cur.id] = cur
                m.fn_def[cur.id] = cur
                blk = None
            else:
                m.global_insts.append(ins)
                if ins.result_id:
                    m.defs[ins.result_id] = ins
        else:
            if n == "OpFunctionParameter":
                if cur.blocks:
                    raise ValueError("OpFunctionParameter after the first "
                                     "block of %%%d" % cur.id)
                cur.params.append(ins)
            elif n == "OpLabel":
                blk = Blk(ins)
                cur.blocks.append(blk)
                cur.block_by_label[blk.label] = blk
            elif n == "OpFunctionEnd":
                if blk is not None and not (blk.insts and
                                            blk.insts[-1].name in sp.TERMINATORS):
                    raise ValueError("last block %%%d of function %%%d has no "
                                     "terminator" % (blk.label, cur.id))
                cur = None
                blk = None
            else:
                if blk is None:
                    raise ValueError("%s in function %%%d outside a block"
                                     % (n, cur.id))
                if blk.insts and blk.insts[-1].name in sp.TERMINATORS:
                    raise ValueError("%s after the terminator of block %%%d"
                                     % (n, blk.label))
                blk.insts.append(ins)
    if cur is not None:
        raise ValueError("function %%%d has no OpFunctionEnd" % cur.id)
    return m


# --- disassembly ------------------------------------------------------------

_ENUMS = {
    "OpCapability": {0: sp.CAPABILITY},
    "OpMemoryModel": {0: sp.ADDRESSING, 1: sp.MEMORY_MODEL},
    "OpEntryPoint": {0: sp.EXEC_MODEL},
    "OpExecutionMode": {1: sp.EXEC_MODE},
    "OpTypePointer": {0: sp.STORAGE},
    "OpVariable": {0: sp.STORAGE},
    "OpDecorate": {1: sp.DECORATION},
    "OpMemberDecorate": {2: sp.DECORATION},
}


def _enum_name(table, value):
    for k in table:
        if table[k] == value:
            return k
    return str(value)


def disassemble(words):
    """Text, one instruction per line, ids shown by their OpName when they
    have one."""
    m = parse(words)
    names = m.names

    def ident(i):
        if i in names:
            return "%" + names[i]
        return "%" + str(i)

    lines = []
    lines.append("; SPIR-V")
    lines.append("; Version: %d.%d" % ((m.version >> 16) & 0xFF,
                                       (m.version >> 8) & 0xFF))
    lines.append("; Bound: %d" % m.bound)
    for ins in m.insts:
        parts = []
        if ins.result_id:
            parts.append(ident(ins.result_id) + " =")
        parts.append(ins.name)
        if ins.type_id:
            parts.append(ident(ins.type_id))
        enums = _ENUMS.get(ins.name, {})
        k = 0
        while k < len(ins.operands):
            kind = ins.operands[k][0]
            val = ins.operands[k][1]
            if kind == "I":
                parts.append(ident(val))
            elif kind == "S":
                parts.append('"%s"' % val)
            elif k in enums:
                parts.append(_enum_name(enums[k], val))
            else:
                parts.append(str(val))
            k = k + 1
        text = " ".join(parts)
        if ins.name == "OpLabel":
            text = "\n" + text
        lines.append(text)
    return "\n".join(lines) + "\n"


# --- validation ---------------------------------------------------------------

def _idoms(entry, order, succs):
    """Immediate dominators over reachable blocks (Cooper, Harvey, Kennedy).
    `order` is a reverse postorder starting at `entry`."""
    index = {}
    for i in range(len(order)):
        index[order[i]] = i
    preds = {}
    for b in order:
        preds[b] = []
    for b in order:
        for s in succs[b]:
            if s in preds:
                preds[s].append(b)
    idom = {entry: entry}
    changed = True
    while changed:
        changed = False
        for b in order:
            if b == entry:
                continue
            new = None
            for p in preds[b]:
                if p in idom:
                    if new is None:
                        new = p
                    else:
                        a = p
                        c = new
                        while a != c:
                            while index[a] > index[c]:
                                a = idom[a]
                            while index[c] > index[a]:
                                c = idom[c]
                        new = a
            if new is not None and idom.get(b) != new:
                idom[b] = new
                changed = True
    return idom, preds


def _rpo(entry, succs):
    seen = {}
    post = []
    stack = [(entry, 0)]
    seen[entry] = True
    while stack:
        node, i = stack.pop()
        ss = succs.get(node, [])
        if i < len(ss):
            stack.append((node, i + 1))
            nxt = ss[i]
            if nxt in succs and nxt not in seen:
                seen[nxt] = True
                stack.append((nxt, 0))
        else:
            post.append(node)
    post.reverse()
    return post


def _dom_set(idom, entry, b):
    out = {}
    cur = b
    while True:
        out[cur] = True
        if cur == entry:
            break
        cur = idom[cur]
    return out


def validate(words):
    """List of error strings; empty means the module passed."""
    errors = []
    if len(words) >= 5:
        if words[4] != 0:
            errors.append("header: schema word must be 0")
    try:
        m = parse(words)
    except ValueError as e:
        return ["decode: " + str(e)]
    _check_header(m, words, errors)
    _check_layout_order(m, errors)
    _check_ids(m, errors)
    _check_global_order(m, errors)
    _check_capabilities(m, errors)
    _check_entry_points(m, errors)
    _check_interface_layout(m, errors)
    for f in m.functions:
        _check_function(m, f, errors)
    _check_recursion(m, errors)
    return errors


def _check_header(m, words, errors):
    if m.version < sp.VERSION_1_0 or m.version > 0x00010600:
        errors.append("header: unsupported version 0x%08x" % m.version)
    maxid = 0
    for ins in m.insts:
        if ins.result_id > maxid:
            maxid = ins.result_id
    if m.bound <= maxid:
        errors.append("header: id bound %d does not exceed the largest "
                      "result id %d" % (m.bound, maxid))
    if m.memory_model is None:
        errors.append("missing OpMemoryModel")
    if not m.capabilities:
        errors.append("no OpCapability: every module declares at least one")


def _check_layout_order(m, errors):
    last = -1
    seen_mm = 0
    for ins in m.insts:
        if ins.name == "OpFunction":
            break
        if ins.name == "OpMemoryModel":
            seen_mm = seen_mm + 1
        sec = sp.SECTION_OF.get(ins.name, sp.SEC_GLOBALS)
        if sec < last:
            errors.append("%s is out of order: module sections must appear "
                          "as capabilities, imports, memory model, entry "
                          "points, execution modes, debug, annotations, "
                          "types/constants/variables, functions" % ins.name)
        if sec > last:
            last = sec
    if seen_mm > 1:
        errors.append("more than one OpMemoryModel")


def _check_ids(m, errors):
    defs = {}
    for ins in m.insts:
        r = ins.result_id
        if r:
            if r in defs:
                errors.append("id %%%d is defined twice" % r)
            defs[r] = ins
            if r >= m.bound:
                errors.append("id %%%d is not below the bound %d"
                              % (r, m.bound))
    m._all_defs = defs
    seen_dec = {}
    for ins in m.insts:
        if ins.name == "OpDecorate" or ins.name == "OpMemberDecorate":
            vals = []
            for op in ins.operands:
                vals.append(op[1])
            key = (ins.name, tuple(vals))
            if key in seen_dec:
                errors.append("%s %s is applied twice" % (ins.name,
                                                          list(key[1])))
            seen_dec[key] = True
    for ins in m.insts:
        if ins.type_id and ins.type_id not in defs:
            errors.append("%s uses undefined type id %%%d"
                          % (ins.name, ins.type_id))
        for op in ins.operands:
            if op[0] == "I" and op[1] not in defs:
                errors.append("%s uses undefined id %%%d" % (ins.name, op[1]))


def _check_global_order(m, errors):
    seen = {}
    for ins in m.global_insts:
        for op in ins.operands:
            if op[0] == "I" and op[1] not in seen:
                errors.append("%s uses %%%d before its definition (types, "
                              "constants and variables must be defined "
                              "before use)" % (ins.name, op[1]))
        if ins.type_id and ins.type_id not in seen:
            errors.append("%s uses type %%%d before its definition"
                          % (ins.name, ins.type_id))
        if ins.result_id:
            seen[ins.result_id] = True
    for fid in m.func_by_id:
        seen[fid] = True


def _type_width_caps(m, errors):
    have = m.capabilities
    caps = sp.CAPABILITY
    for ins in m.global_insts:
        if ins.name == "OpTypeInt":
            w = ins.operands[0][1]
            if w not in (8, 16, 32, 64):
                errors.append("OpTypeInt width %d is not 8, 16, 32 or 64" % w)
            need = {8: "Int8", 16: "Int16", 64: "Int64"}.get(w)
            if need and caps[need] not in have:
                errors.append("%d-bit integer type without capability %s"
                              % (w, need))
        elif ins.name == "OpTypeFloat":
            w = ins.operands[0][1]
            need = {16: "Float16", 64: "Float64"}.get(w)
            if w not in (16, 32, 64):
                errors.append("OpTypeFloat width %d is not 16, 32 or 64" % w)
            if need and caps[need] not in have:
                errors.append("%d-bit float type without capability %s"
                              % (w, need))


def _check_capabilities(m, errors):
    have = m.capabilities
    shader = sp.CAPABILITY["Shader"] in have
    _type_width_caps(m, errors)
    for ep in m.entry_points:
        if ep[0] == sp.EXEC_MODEL["GLCompute"] and not shader:
            errors.append("GLCompute entry point without capability Shader")
    if shader and m.memory_model is not None:
        if m.memory_model[0] != sp.ADDRESSING["Logical"]:
            errors.append("capability Shader requires Logical addressing")
    for ins in m.global_insts:
        if ins.name == "OpVariable":
            sc = ins.operands[0][1]
            if sc == sp.STORAGE["StorageBuffer"] and m.version < sp.VERSION_1_3:
                errors.append("StorageBuffer storage class needs SPIR-V 1.3 "
                              "(or SPV_KHR_storage_buffer_storage_class)")
    for e in m.ext_imports:
        if m.ext_imports[e] not in ("GLSL.std.450",):
            errors.append("unsupported extended instruction set '%s'"
                          % m.ext_imports[e])


def _check_entry_points(m, errors):
    names = {}
    for ep in m.entry_points:
        model, fid, name, iface = ep
        if fid not in m.func_by_id:
            errors.append("entry point '%s' names %%%d, which is not a "
                          "function" % (name, fid))
            continue
        key = (model, name)
        if key in names:
            errors.append("duplicate entry point '%s'" % name)
        names[key] = True
        f = m.func_by_id[fid]
        ft = m._all_defs.get(f.fn_type)
        if ft is not None and ft.name == "OpTypeFunction":
            ret = m._all_defs.get(ft.operands[0][1])
            if ret is None or ret.name != "OpTypeVoid" or len(ft.operands) != 1:
                errors.append("entry point '%s' must have type void()" % name)
        if model == sp.EXEC_MODEL["GLCompute"]:
            modes = m.exec_modes.get(fid, {})
            if sp.EXEC_MODE["LocalSize"] not in modes:
                errors.append("GLCompute entry point '%s' has no LocalSize "
                              "execution mode" % name)
            else:
                a = modes[sp.EXEC_MODE["LocalSize"]]
                if len(a) != 3 or a[0] == 0 or a[1] == 0 or a[2] == 0:
                    errors.append("LocalSize of '%s' must be three non-zero "
                                  "sizes" % name)
        for v in iface:
            d = m._all_defs.get(v)
            if d is None or d.name != "OpVariable":
                errors.append("entry point '%s' interface %%%d is not a "
                              "variable" % (name, v))
    for fid in m.exec_modes:
        if fid not in m.func_by_id:
            errors.append("OpExecutionMode names %%%d, not a function" % fid)


def _scalar_size(inst):
    if inst.name == "OpTypeInt":
        return inst.operands[0][1] // 8
    if inst.name == "OpTypeFloat":
        return inst.operands[0][1] // 8
    return 0


def _decor(m, tid, which):
    for d in m.decorations.get(tid, []):
        if d[0] == sp.DECORATION[which]:
            return d[1]
    return None


def type_layout(m, tid, errors, where, path):
    """(size, scalar alignment) of a buffer-resident type, reporting layout
    errors. Size of a runtime array is its stride (one element)."""
    t = m._all_defs.get(tid)
    if t is None:
        return 0, 1
    if tid in path:
        errors.append("%s: recursive type" % where)
        return 0, 1
    path = path + [tid]
    n = t.name
    if n in ("OpTypeInt", "OpTypeFloat"):
        s = _scalar_size(t)
        return s, s
    if n == "OpTypeBool":
        errors.append("%s: bool has no defined buffer layout" % where)
        return 4, 4
    if n == "OpTypeVector":
        es, ea = type_layout(m, t.operands[0][1], errors, where, path)
        return es * t.operands[1][1], ea
    if n in ("OpTypeArray", "OpTypeRuntimeArray"):
        stride = _decor(m, tid, "ArrayStride")
        es, ea = type_layout(m, t.operands[0][1], errors, where, path)
        if stride is None:
            errors.append("%s: array %%%d has no ArrayStride" % (where, tid))
            return es, ea
        if stride[0] < es:
            errors.append("%s: ArrayStride %d is smaller than its element "
                          "(%d bytes)" % (where, stride[0], es))
        if stride[0] % ea:
            errors.append("%s: ArrayStride %d is not a multiple of the "
                          "element alignment %d" % (where, stride[0], ea))
        if n == "OpTypeRuntimeArray":
            return stride[0], ea
        length = 0
        c = m._all_defs.get(t.operands[1][1])
        if c is not None and c.name == "OpConstant":
            length = c.operands[0][1]
        return stride[0] * length, ea
    if n == "OpTypeStruct":
        end = 0
        align = 1
        k = 0
        while k < len(t.operands):
            md = m.member_decorations.get((tid, k), [])
            off = None
            for d in md:
                if d[0] == sp.DECORATION["Offset"]:
                    off = d[1][0]
            ms, ma = type_layout(m, t.operands[k][1], errors, where, path)
            if off is None:
                errors.append("%s: member %d of struct %%%d has no Offset"
                              % (where, k, tid))
                off = end
            if off % ma:
                errors.append("%s: member %d offset %d is not aligned to %d"
                              % (where, k, off, ma))
            if off < end:
                errors.append("%s: member %d (offset %d) overlaps the "
                              "previous member (ends at %d)"
                              % (where, k, off, end))
            if k + 1 < len(t.operands):
                mt = m._all_defs.get(t.operands[k][1])
                if mt is not None and mt.name == "OpTypeRuntimeArray":
                    errors.append("%s: a runtime array must be the last "
                                  "member" % where)
            end = off + ms
            if ma > align:
                align = ma
            k = k + 1
        return end, align
    errors.append("%s: type %s has no buffer layout" % (where, n))
    return 0, 1


def _check_interface_layout(m, errors):
    bound = {}
    for ins in m.global_insts:
        if ins.name != "OpVariable":
            continue
        sc = ins.operands[0][1]
        if sc not in (sp.STORAGE["StorageBuffer"], sp.STORAGE["Uniform"],
                      sp.STORAGE["PushConstant"]):
            continue
        ptr = m._all_defs.get(ins.type_id)
        if ptr is None or ptr.name != "OpTypePointer":
            errors.append("variable %%%d does not have a pointer type"
                          % ins.result_id)
            continue
        pointee = ptr.operands[1][1]
        where = "variable %%%d" % ins.result_id
        st = m._all_defs.get(pointee)
        if st is None or st.name != "OpTypeStruct":
            errors.append("%s: a buffer variable must point to a struct"
                          % where)
            continue
        need = "Block"
        if sc == sp.STORAGE["Uniform"] and _decor(m, pointee, "BufferBlock") \
                is not None:
            need = "BufferBlock"
        if _decor(m, pointee, need) is None:
            errors.append("%s: struct %%%d is not decorated %s"
                          % (where, pointee, need))
        type_layout(m, pointee, errors, where, [])
        if sc != sp.STORAGE["PushConstant"]:
            s = _decor(m, ins.result_id, "DescriptorSet")
            b = _decor(m, ins.result_id, "Binding")
            if s is None or b is None:
                errors.append("%s: needs both DescriptorSet and Binding"
                              % where)
            else:
                key = (s[0], b[0])
                bound.setdefault(key, []).append(ins.result_id)
    for key in bound:
        ids = bound[key]
        if len(ids) < 2:
            continue
        # Two variables may share a (set, binding) as long as no single entry
        # point can reach both (separate kernels in one module).
        for ep in m.entry_points:
            used = _entry_point_uses(m, ep[1])
            both = [v for v in ids if v in used]
            if len(both) > 1:
                errors.append("variable %%%d: (set %d, binding %d) is already "
                              "used by %%%d in entry point %s"
                              % (both[1], key[0], key[1], both[0], ep[2]))


def _entry_point_uses(m, fid):
    """Ids referenced by an entry point or anything it can call."""
    funcs = {}
    for f in m.functions:
        funcs[f.id] = f
    seen = {}
    used = {}
    todo = [fid]
    while todo:
        cur = todo.pop()
        if cur in seen or cur not in funcs:
            continue
        seen[cur] = True
        for b in funcs[cur].blocks:
            for ins in b.insts:
                for op in ins.operands:
                    if op[0] == "I":
                        used[op[1]] = True
                if ins.name == "OpFunctionCall":
                    todo.append(ins.operands[0][1])
    return used


def _label_operand_slots(ins):
    """Indices into ins.operands that are block labels or forward-allowed
    references, i.e. exempt from the definition-dominates-use check."""
    n = ins.name
    if n == "OpBranch":
        return [0]
    if n == "OpBranchConditional":
        return [1, 2]
    if n == "OpLoopMerge" or n == "OpSelectionMerge":
        return [0] if n == "OpSelectionMerge" else [0, 1]
    if n == "OpSwitch":
        out = [1]
        k = 2
        while k < len(ins.operands):
            if ins.operands[k][0] == "I":
                out.append(k)
            k = k + 1
        return out
    if n == "OpFunctionCall":
        return [0]
    return []


def _check_function(m, f, errors):
    where = "function %%%d" % f.id
    if f.id in m.names:
        where = "function '%s'" % m.names[f.id]
    if not f.blocks:
        errors.append("%s has no blocks (declarations are not supported)"
                      % where)
        return
    # Block-level structure.
    succs = {}
    for b in f.blocks:
        succs[b.label] = b.successors()
    for b in f.blocks:
        for s in succs[b.label]:
            if s not in f.block_by_label:
                errors.append("%s: block %%%d branches to %%%d, which is not "
                              "a block of this function" % (where, b.label, s))
    entry = f.blocks[0].label
    order = _rpo(entry, succs)
    reach = {}
    for b in order:
        reach[b] = True
    idom, preds = _idoms(entry, order, succs)
    # OpVariable placement: Function-storage variables first in the entry
    # block, nowhere else.
    for bi in range(len(f.blocks)):
        seen_other = False
        for ins in f.blocks[bi].insts:
            if ins.name == "OpVariable":
                if bi != 0:
                    errors.append("%s: OpVariable outside the entry block"
                                  % where)
                elif seen_other:
                    errors.append("%s: OpVariable must precede every other "
                                  "instruction of the entry block" % where)
                if ins.operands[0][1] != sp.STORAGE["Function"]:
                    errors.append("%s: only Function storage variables may "
                                  "appear in a function" % where)
            else:
                seen_other = True
    # Definition dominates use.
    defpos = {}
    for bi in range(len(f.blocks)):
        b = f.blocks[bi]
        for k in range(len(b.insts)):
            if b.insts[k].result_id:
                defpos[b.insts[k].result_id] = (b.label, k)
    params = {}
    for p in f.params:
        params[p.result_id] = True
    doms = {}
    for b in order:
        doms[b] = _dom_set(idom, entry, b)
    for b in f.blocks:
        if b.label not in reach:
            continue
        for k in range(len(b.insts)):
            ins = b.insts[k]
            if ins.name == "OpPhi":
                pairs = ins.operands
                got = {}
                q = 0
                while q + 1 < len(pairs):
                    val = pairs[q][1]
                    par = pairs[q + 1][1]
                    got[par] = True
                    if par not in preds[b.label] and par in reach:
                        errors.append("%s: OpPhi %%%d lists %%%d, which is "
                                      "not a predecessor of block %%%d"
                                      % (where, ins.result_id, par, b.label))
                    if val in defpos and par in reach:
                        db = defpos[val][0]
                        if db not in doms[par]:
                            errors.append("%s: OpPhi %%%d operand %%%d does "
                                          "not dominate the end of "
                                          "predecessor %%%d" % (where,
                                                                ins.result_id,
                                                                val, par))
                    q = q + 2
                for p in preds[b.label]:
                    if p not in got:
                        errors.append("%s: OpPhi %%%d has no operand for "
                                      "predecessor %%%d" % (where,
                                                            ins.result_id, p))
                for kk in range(k):
                    if b.insts[kk].name != "OpPhi":
                        errors.append("%s: OpPhi %%%d is not at the start of "
                                      "its block" % (where, ins.result_id))
                        break
                continue
            exempt = _label_operand_slots(ins)
            for oi in range(len(ins.operands)):
                kind = ins.operands[oi][0]
                if kind != "I" or oi in exempt:
                    continue
                v = ins.operands[oi][1]
                if v in defpos:
                    db = defpos[v][0]
                    dk = defpos[v][1]
                    if db == b.label:
                        if dk >= k:
                            errors.append("%s: %s uses %%%d before its "
                                          "definition in block %%%d"
                                          % (where, ins.name, v, b.label))
                    elif db not in doms[b.label]:
                        errors.append("%s: %s uses %%%d, whose definition in "
                                      "block %%%d does not dominate block "
                                      "%%%d" % (where, ins.name, v, db,
                                                b.label))
            if ins.name == "OpFunctionCall":
                callee = ins.operands[0][1]
                if callee not in m.func_by_id:
                    errors.append("%s: OpFunctionCall target %%%d is not a "
                                  "function" % (where, callee))
    _check_structured(m, f, where, succs, order, reach, idom, doms, errors)


def _check_structured(m, f, where, succs, order, reach, idom, doms, errors):
    entry = f.blocks[0].label
    header = {}      # label -> ("loop", merge, cont) | ("sel", merge)
    merge_of = {}
    for b in f.blocks:
        t = b.terminator()
        k = len(b.insts) - 2
        mi = None
        if k >= 0 and b.insts[k].name in ("OpLoopMerge", "OpSelectionMerge"):
            mi = b.insts[k]
        for kk in range(len(b.insts) - 1):
            if b.insts[kk].name in ("OpLoopMerge", "OpSelectionMerge") \
                    and kk != k:
                errors.append("%s: block %%%d: a merge instruction must be "
                              "the second-to-last instruction"
                              % (where, b.label))
        if mi is None:
            continue
        if mi.name == "OpLoopMerge":
            mg = mi.operands[0][1]
            ct = mi.operands[1][1]
            header[b.label] = ("loop", mg, ct)
            if t.name not in ("OpBranch", "OpBranchConditional"):
                errors.append("%s: loop header %%%d must end in OpBranch or "
                              "OpBranchConditional" % (where, b.label))
            if ct not in f.block_by_label:
                errors.append("%s: loop %%%d continue target %%%d is not a "
                              "block" % (where, b.label, ct))
        else:
            mg = mi.operands[0][1]
            header[b.label] = ("sel", mg)
            if t.name not in ("OpBranchConditional", "OpSwitch"):
                errors.append("%s: selection header %%%d must end in "
                              "OpBranchConditional or OpSwitch"
                              % (where, b.label))
        if mg not in f.block_by_label:
            errors.append("%s: merge block %%%d of %%%d is not a block"
                          % (where, mg, b.label))
            continue
        if mg == b.label:
            errors.append("%s: block %%%d is its own merge block"
                          % (where, b.label))
        if mg in merge_of:
            errors.append("%s: block %%%d is the merge block of both %%%d "
                          "and %%%d" % (where, mg, merge_of[mg], b.label))
        merge_of[mg] = b.label
        if b.label in reach and mg in reach and b.label not in doms[mg]:
            errors.append("%s: header %%%d does not dominate its merge block "
                          "%%%d" % (where, b.label, mg))
        if mi.name == "OpLoopMerge":
            ct = header[b.label][2]
            if b.label in reach and ct in reach and b.label not in doms[ct]:
                errors.append("%s: loop header %%%d does not dominate its "
                              "continue target %%%d" % (where, b.label, ct))
    cont_of = {}
    for h in header:
        if header[h][0] == "loop":
            cont_of[header[h][2]] = h
    # Back edges.
    for b in f.blocks:
        if b.label not in reach:
            continue
        for s in succs[b.label]:
            if s in reach and s in doms[b.label]:
                if s not in header or header[s][0] != "loop":
                    errors.append("%s: block %%%d branches back to %%%d, "
                                  "which is not a loop header"
                                  % (where, b.label, s))
                elif header[s][2] != b.label:
                    errors.append("%s: back edge %%%d -> %%%d does not come "
                                  "from the loop's continue target %%%d"
                                  % (where, b.label, s, header[s][2]))
    for h in header:
        if header[h][0] == "loop" and h in reach:
            ct = header[h][2]
            if ct in reach and h not in succs[ct]:
                errors.append("%s: continue target %%%d of loop %%%d does not "
                              "branch back to the header" % (where, ct, h))
    # Constructs and exits.
    constructs = {}
    for h in header:
        if h not in reach or header[h][0] != "loop":
            continue
        mg = header[h][1]
        members = {}
        # A loop is everything it dominates up to its merge block.
        for b in order:
            if h in doms[b] and not (mg in reach and mg in doms[b]):
                members[b] = True
        constructs[h] = members
    for h in header:
        if h not in reach or header[h][0] == "loop":
            continue
        mg = header[h][1]
        # A selection is what is reached from the header before leaving
        # through its merge block or through a break / continue of a loop
        # around it: those end the construct, so the loop's merge block (and
        # whatever follows it) is not part of it even when only a break
        # inside the selection reaches it.
        stop = {}
        for oh in constructs:
            if header[oh][0] == "loop" and h in constructs[oh]:
                stop[header[oh][1]] = True
                stop[header[oh][2]] = True
        members = {}
        todo = [h]
        while todo:
            b = todo.pop()
            if b in members:
                continue
            members[b] = True
            for s in succs[b]:
                if s == mg or s in stop or s in members:
                    continue
                if s not in reach or h not in doms[s]:
                    continue
                todo.append(s)
        constructs[h] = members
    for h in constructs:
        mg = header[h][1]
        allowed = {}
        allowed[mg] = True
        # The innermost enclosing loop's merge and continue are the legal
        # exits (break, continue); so is the back edge itself.
        inner = None
        for oh in header:
            if oh == h or oh not in constructs:
                continue
            if header[oh][0] == "loop" and h in constructs[oh]:
                if inner is None or len(constructs[oh]) < len(constructs[inner]):
                    inner = oh
        # Only a selection can break or continue an enclosing loop; a loop
        # inside a loop leaves through its own merge block.
        if inner is not None and header[h][0] != "loop":
            allowed[header[inner][1]] = True
            allowed[header[inner][2]] = True
            allowed[inner] = True
        if header[h][0] == "loop":
            allowed[h] = True
        for b in constructs[h]:
            for s in succs[b]:
                if s not in constructs[h] and s not in allowed:
                    errors.append("%s: block %%%d inside the construct of "
                                  "header %%%d branches to %%%d, which is "
                                  "neither its merge block nor an enclosing "
                                  "loop's break/continue target"
                                  % (where, b, h, s))
    # Every conditional branch needs a merge, with the spec's exemptions.
    for b in f.blocks:
        if b.label not in reach:
            continue
        t = b.terminator()
        if t.name not in ("OpBranchConditional", "OpSwitch"):
            continue
        if b.label in header or b.label in cont_of:
            continue
        exempt = False
        if t.name == "OpBranchConditional":
            targets = [t.operands[1][1], t.operands[2][1]]
            for oh in header:
                if header[oh][0] == "loop" and oh in constructs and \
                        b.label in constructs[oh]:
                    if header[oh][1] in targets or header[oh][2] in targets:
                        exempt = True
        if not exempt:
            errors.append("%s: block %%%d ends in a conditional branch but "
                          "has no OpSelectionMerge or OpLoopMerge"
                          % (where, b.label))
    # Shader: terminators other than the structured set are fine; check
    # OpUnreachable blocks have no successors by construction.


def _check_recursion(m, errors):
    calls = {}
    for f in m.functions:
        calls[f.id] = []
        for b in f.blocks:
            for ins in b.insts:
                if ins.name == "OpFunctionCall":
                    calls[f.id].append(ins.operands[0][1])
    state = {}
    reported = {}

    def visit(fid, stack):
        state[fid] = 1
        for c in calls.get(fid, []):
            if c not in calls:
                continue
            if state.get(c) == 1:
                key = (fid, c)
                if key not in reported:
                    reported[key] = True
                    errors.append("recursion: function %%%d calls %%%d, which "
                                  "is already on the call stack (Vulkan "
                                  "forbids recursion)" % (fid, c))
            elif state.get(c) is None:
                visit(c, stack + [c])
        state[fid] = 2

    for fid in calls:
        if state.get(fid) is None:
            visit(fid, [fid])


def main(argv):
    if len(argv) < 2:
        print("usage: spirv_reader.py FILE.spv [--dis] [--no-validate]")
        return 2
    path = argv[1]
    f = open(path, "rb")
    data = f.read()
    f.close()
    words = sp.bytes_to_words(data)
    if "--dis" in argv:
        sys.stdout.write(disassemble(words))
    if "--no-validate" not in argv:
        errs = validate(words)
        if errs:
            for e in errs:
                print("error: " + e)
            return 1
        print("ok: %s validates" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
