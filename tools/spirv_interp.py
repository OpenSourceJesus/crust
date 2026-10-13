#!/usr/bin/env python3
"""Reference interpreter for the SPIR-V subset the Crust back end emits.

There is no GPU and no Vulkan driver in the build, and there should not need
to be one to know whether a kernel is right. This runs a compute entry point
over a dispatch grid on the CPU, with real workgroup semantics -- shared
`Workgroup` memory, `OpControlBarrier` -- so a kernel's output buffers can be
compared with the same source built natively (tools/spirv_difftest.py).

It is deliberately *strict*: where SPIR-V says a result is undefined, this
raises `InterpError` rather than picking a value, so the back end has to emit
the explicit guard. That covers a shift count at or past the operand width,
integer division or remainder by zero, a float-to-integer conversion whose
result does not fit, an out-of-range buffer access, a store through a
`NonWritable` buffer, `OpUnreachable`, and a barrier that only part of a
workgroup reaches. (The wasm back end made the opposite, documented choice for
float-to-int -- saturate -- and the difftest carries it as a known divergence;
here the *lowering* decides, not the interpreter.)

Values: integers are held as unsigned Python ints in [0, 2**width) -- SPIR-V
integers are signless, the operation decides signedness. Floats are Python
floats rounded to their width after every operation. Vectors, arrays and
structs are Python lists. A pointer is a (cell, path) pair.

Math functions (`GLSL.std.450`) are libm in double, rounded to the result
width. Hardware transcendentals differ by a few ULP; callers compare floats
within a stated bound.

    python3 tools/spirv_interp.py prog.spv main 64,1,1 0=in.bin 1=out.bin
"""

import copy
import math
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

import shivyc.spirv as sp
import shivyc.spirv_reader as rd

STEP_LIMIT = 20000000


class InterpError(Exception):
    pass


class Ty:
    def __init__(self, kind):
        self.kind = kind
        self.width = 0
        self.signed = False
        self.elem = 0
        self.count = 0
        self.members = []
        self.sc = 0
        self.pointee = 0


class Ptr:
    def __init__(self, cell, path, var):
        self.cell = cell
        self.path = path
        self.var = var


def _mask(w):
    return (1 << w) - 1


def _sgn(a, w):
    if a >> (w - 1):
        return a - (1 << w)
    return a


def _rf(x, w):
    """Round a Python float to a float type's width."""
    if w == 64:
        return x
    if x != x or x in (math.inf, -math.inf):
        return x
    fmt = "<f" if w == 32 else "<e"
    try:
        return struct.unpack(fmt, struct.pack(fmt, x))[0]
    except OverflowError:
        return math.copysign(math.inf, x)


def _map1(f, a):
    if isinstance(a, list):
        out = []
        for x in a:
            out.append(_map1(f, x))
        return out
    return f(a)


def _map2(f, a, b):
    if isinstance(a, list):
        out = []
        for i in range(len(a)):
            out.append(_map2(f, a[i], b[i]))
        return out
    return f(a, b)


def _fdiv(a, b):
    if b == 0.0:
        if a == 0.0 or a != a:
            return math.nan
        neg = (math.copysign(1.0, a) < 0) != (math.copysign(1.0, b) < 0)
        return -math.inf if neg else math.inf
    return a / b


def _fmul(a, b):
    return a * b


def _fadd(a, b):
    return a + b


def _frem(a, b):
    try:
        return math.fmod(a, b)
    except ValueError:
        return math.nan


def _fmod(a, b):
    r = _frem(a, b)
    if r == r and r != 0.0 and ((r < 0) != (b < 0)):
        r = r + b
    return r


def _safe(f):
    def g(*a):
        try:
            return f(*a)
        except (ValueError, ZeroDivisionError):
            return math.nan
        except OverflowError:
            return math.inf
    return g


def _fmin(a, b):
    if a != a:
        return b
    if b != b:
        return a
    return b if b < a else a


def _fmax(a, b):
    if a != a:
        return b
    if b != b:
        return a
    return b if b > a else a


def _round_even(x):
    if x != x or x in (math.inf, -math.inf):
        return x
    return float(round(x))


def _fsign(x):
    if x != x:
        return x
    if x > 0:
        return 1.0
    if x < 0:
        return -1.0
    return 0.0


# GLSL.std.450 float functions: code -> (arity, function)
_GLSL_F = {
    1: (1, lambda x: float(math.floor(x + 0.5)) if x == x and abs(x) != math.inf else x),
    2: (1, _round_even),
    3: (1, lambda x: float(math.trunc(x)) if x == x and abs(x) != math.inf else x),
    4: (1, abs),
    6: (1, _fsign),
    8: (1, lambda x: float(math.floor(x)) if x == x and abs(x) != math.inf else x),
    9: (1, lambda x: float(math.ceil(x)) if x == x and abs(x) != math.inf else x),
    10: (1, lambda x: x - math.floor(x) if x == x and abs(x) != math.inf else math.nan),
    13: (1, _safe(math.sin)),
    14: (1, _safe(math.cos)),
    15: (1, _safe(math.tan)),
    25: (2, _safe(math.atan2)),
    26: (2, _safe(math.pow)),
    27: (1, _safe(math.exp)),
    28: (1, _safe(math.log)),
    29: (1, _safe(lambda x: 2.0 ** x)),
    30: (1, _safe(math.log2)),
    31: (1, _safe(math.sqrt)),
    32: (1, _safe(lambda x: 1.0 / math.sqrt(x))),
    37: (2, _fmin),
    40: (2, _fmax),
    43: (3, lambda x, lo, hi: _fmin(_fmax(x, lo), hi)),
    50: (3, _safe(lambda a, b, c: math.fma(a, b, c))),
}


class Machine:
    def __init__(self, words):
        self.words = words
        self.m = rd.parse(words)
        self.types = {}
        self.consts = {}
        self.rtype = {}
        self.gvars = {}          # var id -> (storage class, pointee type id)
        self._build()

    # --- module tables ---------------------------------------------------

    def zero(self, tid):
        t = self.types[tid]
        k = t.kind
        if k == "int":
            return 0
        if k == "float":
            return 0.0
        if k == "bool":
            return False
        if k in ("vec", "array"):
            out = []
            for _i in range(t.count):
                out.append(self.zero(t.elem))
            return out
        if k == "struct":
            out = []
            for mt in t.members:
                out.append(self.zero(mt))
            return out
        if k == "rtarray":
            return []
        raise InterpError("no zero value for type kind " + k)

    def _build(self):
        m = self.m
        for ins in m.global_insts:
            n = ins.name
            r = ins.result_id
            if ins.type_id:
                self.rtype[r] = ins.type_id
            if n == "OpTypeVoid":
                self.types[r] = Ty("void")
            elif n == "OpTypeBool":
                self.types[r] = Ty("bool")
            elif n == "OpTypeInt":
                t = Ty("int")
                t.width = ins.operands[0][1]
                t.signed = bool(ins.operands[1][1])
                self.types[r] = t
            elif n == "OpTypeFloat":
                t = Ty("float")
                t.width = ins.operands[0][1]
                self.types[r] = t
            elif n == "OpTypeVector":
                t = Ty("vec")
                t.elem = ins.operands[0][1]
                t.count = ins.operands[1][1]
                self.types[r] = t
            elif n == "OpTypeArray":
                t = Ty("array")
                t.elem = ins.operands[0][1]
                t.count = self.consts[ins.operands[1][1]]
                self.types[r] = t
            elif n == "OpTypeRuntimeArray":
                t = Ty("rtarray")
                t.elem = ins.operands[0][1]
                self.types[r] = t
            elif n == "OpTypeStruct":
                t = Ty("struct")
                for op in ins.operands:
                    t.members.append(op[1])
                self.types[r] = t
            elif n == "OpTypePointer":
                t = Ty("ptr")
                t.sc = ins.operands[0][1]
                t.pointee = ins.operands[1][1]
                self.types[r] = t
            elif n == "OpTypeFunction":
                self.types[r] = Ty("fn")
            elif n == "OpConstant":
                t = self.types[ins.type_id]
                w = ins.operands[0][1]
                if len(ins.operands) > 1:
                    w = w | (ins.operands[1][1] << 32)
                if t.kind == "int":
                    self.consts[r] = w & _mask(t.width)
                else:
                    self.consts[r] = self._float_bits(t.width, w)
            elif n == "OpConstantTrue":
                self.consts[r] = True
            elif n == "OpConstantFalse":
                self.consts[r] = False
            elif n == "OpConstantComposite":
                parts = []
                for op in ins.operands:
                    parts.append(self.consts[op[1]])
                self.consts[r] = parts
            elif n == "OpConstantNull" or n == "OpUndef":
                self.consts[r] = self.zero(ins.type_id)
            elif n == "OpVariable":
                pt = self.types[ins.type_id]
                self.gvars[r] = (ins.operands[0][1], pt.pointee)
        for f in m.functions:
            for p in f.params:
                self.rtype[p.result_id] = p.type_id
            for b in f.blocks:
                for ins in b.insts:
                    if ins.result_id and ins.type_id:
                        self.rtype[ins.result_id] = ins.type_id

    def _float_bits(self, width, bits):
        if width == 32:
            return struct.unpack("<f", struct.pack("<I", bits))[0]
        if width == 64:
            return struct.unpack("<d", struct.pack("<Q", bits))[0]
        return struct.unpack("<e", struct.pack("<H", bits & 0xFFFF))[0]

    def scalar(self, tid):
        t = self.types[tid]
        while t.kind == "vec":
            t = self.types[t.elem]
        return t

    # --- buffer layout ---------------------------------------------------

    def _decor(self, tid, which):
        for d in self.m.decorations.get(tid, []):
            if d[0] == sp.DECORATION[which]:
                return d[1]
        return None

    def _member_off(self, sid, k):
        for d in self.m.member_decorations.get((sid, k), []):
            if d[0] == sp.DECORATION["Offset"]:
                return d[1][0]
        raise InterpError("struct %%%d member %d has no Offset" % (sid, k))

    def sizeof(self, tid):
        t = self.types[tid]
        if t.kind in ("int", "float"):
            return t.width // 8
        if t.kind == "vec":
            return t.count * self.sizeof(t.elem)
        if t.kind == "array":
            return t.count * self._decor(tid, "ArrayStride")[0]
        if t.kind == "struct":
            end = 0
            for k in range(len(t.members)):
                mt = self.types[t.members[k]]
                if mt.kind == "rtarray":
                    sz = self._decor(t.members[k], "ArrayStride")[0]
                else:
                    sz = self.sizeof(t.members[k])
                e = self._member_off(tid, k) + sz
                if e > end:
                    end = e
            return end
        raise InterpError("no size for type kind " + t.kind)

    def unpack(self, tid, buf, off):
        t = self.types[tid]
        k = t.kind
        if k == "int":
            n = t.width // 8
            return int.from_bytes(bytes(buf[off:off + n]), "little")
        if k == "float":
            fmt = {16: "<e", 32: "<f", 64: "<d"}[t.width]
            n = t.width // 8
            return struct.unpack(fmt, bytes(buf[off:off + n]))[0]
        if k == "vec":
            es = self.sizeof(t.elem)
            out = []
            for i in range(t.count):
                out.append(self.unpack(t.elem, buf, off + i * es))
            return out
        if k == "array":
            stride = self._decor(tid, "ArrayStride")[0]
            out = []
            for i in range(t.count):
                out.append(self.unpack(t.elem, buf, off + i * stride))
            return out
        if k == "rtarray":
            stride = self._decor(tid, "ArrayStride")[0]
            n = (len(buf) - off) // stride
            out = []
            for i in range(n):
                out.append(self.unpack(t.elem, buf, off + i * stride))
            return out
        if k == "struct":
            out = []
            for i in range(len(t.members)):
                out.append(self.unpack(t.members[i], buf,
                                       off + self._member_off(tid, i)))
            return out
        raise InterpError("cannot unpack type kind " + k)

    def pack(self, tid, val, buf, off):
        t = self.types[tid]
        k = t.kind
        if k == "int":
            n = t.width // 8
            buf[off:off + n] = (val & _mask(t.width)).to_bytes(n, "little")
        elif k == "float":
            fmt = {16: "<e", 32: "<f", 64: "<d"}[t.width]
            n = t.width // 8
            buf[off:off + n] = struct.pack(fmt, val)
        elif k == "vec":
            es = self.sizeof(t.elem)
            for i in range(t.count):
                self.pack(t.elem, val[i], buf, off + i * es)
        elif k in ("array", "rtarray"):
            stride = self._decor(tid, "ArrayStride")[0]
            for i in range(len(val)):
                self.pack(t.elem, val[i], buf, off + i * stride)
        elif k == "struct":
            for i in range(len(t.members)):
                self.pack(t.members[i], val[i], buf,
                          off + self._member_off(tid, i))
        else:
            raise InterpError("cannot pack type kind " + k)

    # --- dispatch --------------------------------------------------------

    def run(self, entry, grid, buffers, push=b""):
        ep = None
        for e in self.m.entry_points:
            if e[2] == entry and e[0] == sp.EXEC_MODEL["GLCompute"]:
                ep = e
        if ep is None:
            raise InterpError("no GLCompute entry point named '%s'" % entry)
        fn = self.m.func_by_id[ep[1]]
        ls = self.m.exec_modes[ep[1]][sp.EXEC_MODE["LocalSize"]]
        self.steps = 0
        dispatch = {}
        bufmap = {}
        for key in buffers:
            k = key
            if not isinstance(k, tuple):
                k = (0, k)
            bufmap[k] = buffers[key]
        for vid in self.gvars:
            sc, pointee = self.gvars[vid]
            if sc in (sp.STORAGE["StorageBuffer"], sp.STORAGE["Uniform"]):
                s = self._decor(vid, "DescriptorSet")
                b = self._decor(vid, "Binding")
                key = (s[0], b[0])
                if key not in bufmap:
                    raise InterpError("no buffer bound at set %d binding %d"
                                      % key)
                dispatch[vid] = [self.unpack(pointee, bufmap[key], 0)]
            elif sc == sp.STORAGE["PushConstant"]:
                dispatch[vid] = [self.unpack(pointee, bytearray(push), 0)]
        gx, gy, gz = grid
        for z in range(gz):
            for y in range(gy):
                for x in range(gx):
                    self._run_group(fn, ls, (x, y, z), grid, dispatch)
        for vid in dispatch:
            sc, pointee = self.gvars[vid]
            if sc == sp.STORAGE["StorageBuffer"] or \
                    sc == sp.STORAGE["Uniform"]:
                key = (self._decor(vid, "DescriptorSet")[0],
                       self._decor(vid, "Binding")[0])
                self.pack(pointee, dispatch[vid][0], bufmap[key], 0)
        return {"steps": self.steps}

    def _run_group(self, fn, ls, wg, grid, dispatch):
        shared = {}
        for vid in self.gvars:
            sc, pointee = self.gvars[vid]
            if sc == sp.STORAGE["Workgroup"]:
                shared[vid] = [self.zero(pointee)]
        gens = []
        for lz in range(ls[2]):
            for ly in range(ls[1]):
                for lx in range(ls[0]):
                    lid = (lx, ly, lz)
                    index = (lz * ls[1] + ly) * ls[0] + lx
                    cells = {}
                    cells.update(dispatch)
                    cells.update(shared)
                    for vid in self.gvars:
                        sc, pointee = self.gvars[vid]
                        if sc in (sp.STORAGE["Private"],
                                  sp.STORAGE["Output"]):
                            cells[vid] = [self.zero(pointee)]
                        elif sc == sp.STORAGE["Input"]:
                            cells[vid] = [self._builtin(vid, wg, lid, ls,
                                                        grid, index)]
                    gens.append(self._invocation(fn, cells))
        alive = list(range(len(gens)))
        while alive:
            waiting = []
            done = 0
            for i in alive:
                try:
                    next(gens[i])
                    waiting.append(i)
                except StopIteration:
                    done = done + 1
            if waiting and done:
                raise InterpError("a barrier was reached by only part of the "
                                  "workgroup (divergent control barrier: "
                                  "undefined)")
            alive = waiting

    def _builtin(self, vid, wg, lid, ls, grid, index):
        bi = None
        for d in self.m.decorations.get(vid, []):
            if d[0] == sp.DECORATION["BuiltIn"]:
                bi = d[1][0]
        if bi == sp.BUILTIN["GlobalInvocationId"]:
            return [wg[0] * ls[0] + lid[0], wg[1] * ls[1] + lid[1],
                    wg[2] * ls[2] + lid[2]]
        if bi == sp.BUILTIN["LocalInvocationId"]:
            return list(lid)
        if bi == sp.BUILTIN["WorkgroupId"]:
            return list(wg)
        if bi == sp.BUILTIN["NumWorkgroups"]:
            return list(grid)
        if bi == sp.BUILTIN["LocalInvocationIndex"]:
            return index
        raise InterpError("unsupported Input variable %%%d (BuiltIn %s)"
                          % (vid, bi))

    def _invocation(self, fn, cells):
        yield from self._call(fn, [], cells)

    # --- execution -------------------------------------------------------

    def _walk(self, ptr):
        v = ptr.cell[0]
        for i in ptr.path:
            if not isinstance(i, int) or i < 0 or i >= len(v):
                raise InterpError("out-of-bounds access: index %s of a "
                                  "%d-element object" % (i, len(v)))
            v = v[i]
        return v

    def _store(self, ptr, value):
        if ptr.var in self.gvars:
            if self._decor(ptr.var, "NonWritable") is not None:
                raise InterpError("store through NonWritable variable %%%d"
                                  % ptr.var)
            sc, pointee = self.gvars[ptr.var]
            if sc == sp.STORAGE["PushConstant"]:
                raise InterpError("store to a push constant")
            if ptr.path and self.types[pointee].kind == "struct":
                for d in self.m.member_decorations.get(
                        (pointee, ptr.path[0]), []):
                    if d[0] == sp.DECORATION["NonWritable"]:
                        raise InterpError("store to a NonWritable member")
        value = copy.deepcopy(value)
        if not ptr.path:
            ptr.cell[0] = value
            return
        parent = Ptr(ptr.cell, ptr.path[:-1], ptr.var)
        container = self._walk(parent)
        i = ptr.path[-1]
        if not isinstance(i, int) or i < 0 or i >= len(container):
            raise InterpError("out-of-bounds store: index %s of a "
                              "%d-element object" % (i, len(container)))
        container[i] = value

    def _call(self, fn, args, cells):
        vals = {}
        for i in range(len(fn.params)):
            vals[fn.params[i].result_id] = args[i]
        consts = self.consts

        def get(i):
            if i in vals:
                return vals[i]
            if i in consts:
                return consts[i]
            if i in cells:
                return Ptr(cells[i], (), i)
            raise InterpError("use of %%%d before it has a value" % i)

        label = fn.blocks[0].label
        prev = None
        while True:
            blk = fn.block_by_label[label]
            insts = blk.insts
            k = 0
            pend = []
            while k < len(insts) and insts[k].name == "OpPhi":
                ins = insts[k]
                pick = None
                q = 0
                while q + 1 < len(ins.operands):
                    if ins.operands[q + 1][1] == prev:
                        pick = ins.operands[q][1]
                    q = q + 2
                if pick is None:
                    raise InterpError("OpPhi %%%d has no value for "
                                      "predecessor %%%s" % (ins.result_id,
                                                            prev))
                pend.append((ins.result_id, get(pick)))
                k = k + 1
            for rid, v in pend:
                vals[rid] = v
            nxt = None
            while k < len(insts):
                ins = insts[k]
                k = k + 1
                self.steps = self.steps + 1
                if self.steps > STEP_LIMIT:
                    raise InterpError("step limit exceeded (infinite loop?)")
                n = ins.name
                o = ins.operands
                if n == "OpBranch":
                    nxt = o[0][1]
                elif n == "OpBranchConditional":
                    nxt = o[1][1] if get(o[0][1]) else o[2][1]
                elif n == "OpSwitch":
                    sel = get(o[0][1])
                    nxt = o[1][1]
                    q = 2
                    while q + 1 < len(o):
                        if o[q][1] == sel:
                            nxt = o[q + 1][1]
                        q = q + 2
                elif n == "OpReturn":
                    return None
                elif n == "OpReturnValue":
                    return get(o[0][1])
                elif n == "OpUnreachable":
                    raise InterpError("OpUnreachable executed")
                elif n == "OpLoopMerge" or n == "OpSelectionMerge" or \
                        n == "OpMemoryBarrier":
                    pass
                elif n == "OpControlBarrier":
                    yield "barrier"
                elif n == "OpStore":
                    self._store(get(o[0][1]), get(o[1][1]))
                elif n == "OpFunctionCall":
                    callee = self.m.func_by_id[o[0][1]]
                    cargs = []
                    for q in range(1, len(o)):
                        cargs.append(get(o[q][1]))
                    res = yield from self._call(callee, cargs, cells)
                    vals[ins.result_id] = res
                else:
                    vals[ins.result_id] = self._value(ins, get, vals)
            label_next = nxt
            prev = label
            label = label_next

    def _value(self, ins, get, vals):
        n = ins.name
        o = ins.operands
        rt = ins.type_id
        if n == "OpVariable":
            pt = self.types[rt]
            if len(o) > 1:
                init = copy.deepcopy(get(o[1][1]))
            else:
                init = self.zero(pt.pointee)
            return Ptr([init], (), None)
        if n == "OpLoad":
            return copy.deepcopy(self._walk(get(o[0][1])))
        if n == "OpAccessChain" or n == "OpInBoundsAccessChain":
            base = get(o[0][1])
            path = base.path
            for q in range(1, len(o)):
                path = path + (get(o[q][1]),)
            return Ptr(base.cell, path, base.var)
        if n == "OpArrayLength":
            base = get(o[0][1])
            arr = self._walk(Ptr(base.cell, base.path + (o[1][1],),
                                 base.var))
            return len(arr)
        if n == "OpCopyObject":
            return get(o[0][1])
        if n == "OpUndef" or n == "OpConstantNull":
            return self.zero(rt)
        if n == "OpCompositeConstruct":
            parts = []
            for op in o:
                parts.append(get(op[1]))
            if self.types[rt].kind == "vec":
                flat = []
                for p in parts:
                    if isinstance(p, list):
                        flat.extend(p)
                    else:
                        flat.append(p)
                return flat
            return parts
        if n == "OpCompositeExtract":
            v = get(o[0][1])
            for q in range(1, len(o)):
                v = v[o[q][1]]
            return copy.deepcopy(v)
        if n == "OpCompositeInsert":
            obj = get(o[0][1])
            comp = copy.deepcopy(get(o[1][1]))
            idx = []
            for q in range(2, len(o)):
                idx.append(o[q][1])
            cur = comp
            for q in range(len(idx) - 1):
                cur = cur[idx[q]]
            cur[idx[-1]] = copy.deepcopy(obj)
            return comp
        if n == "OpVectorShuffle":
            a = get(o[0][1])
            b = get(o[1][1])
            both = a + b
            out = []
            for q in range(2, len(o)):
                out.append(0 if o[q][1] == 0xFFFFFFFF else both[o[q][1]])
            return out
        if n == "OpSelect":
            c = get(o[0][1])
            a = get(o[1][1])
            b = get(o[2][1])
            if isinstance(c, list):
                out = []
                for i in range(len(c)):
                    out.append(a[i] if c[i] else b[i])
                return out
            return a if c else b
        if n == "OpExtInst":
            return self._ext(ins, get)
        return self._arith(ins, get)

    def _arith(self, ins, get, ):
        n = ins.name
        o = ins.operands
        rt = ins.type_id
        # ----- conversions and unary -----
        if n in ("OpSNegate", "OpNot", "OpFNegate", "OpLogicalNot",
                 "OpConvertFToS", "OpConvertFToU", "OpConvertSToF",
                 "OpConvertUToF", "OpUConvert", "OpSConvert", "OpFConvert",
                 "OpBitcast"):
            a = get(o[0][1])
            rs = self.scalar(rt)
            src = self.scalar(self.rtype[o[0][1]])
            if n == "OpSNegate":
                return _map1(lambda x: (-x) & _mask(rs.width), a)
            if n == "OpNot":
                return _map1(lambda x: (~x) & _mask(rs.width), a)
            if n == "OpFNegate":
                return _map1(lambda x: -x, a)
            if n == "OpLogicalNot":
                return _map1(lambda x: not x, a)
            if n == "OpConvertFToS" or n == "OpConvertFToU":
                signed = (n == "OpConvertFToS")

                def f2i(x):
                    if x != x or abs(x) == math.inf:
                        raise InterpError("%s of NaN or infinity is "
                                          "undefined" % n)
                    v = math.trunc(x)
                    lo = -(1 << (rs.width - 1)) if signed else 0
                    hi = (1 << (rs.width - 1)) - 1 if signed \
                        else (1 << rs.width) - 1
                    if v < lo or v > hi:
                        raise InterpError("%s: %r does not fit in %d bits "
                                          "(undefined)" % (n, x, rs.width))
                    return v & _mask(rs.width)
                return _map1(f2i, a)
            if n == "OpConvertSToF":
                return _map1(lambda x: _rf(float(_sgn(x, src.width)),
                                           rs.width), a)
            if n == "OpConvertUToF":
                return _map1(lambda x: _rf(float(x), rs.width), a)
            if n == "OpUConvert":
                return _map1(lambda x: x & _mask(rs.width), a)
            if n == "OpSConvert":
                return _map1(lambda x: _sgn(x, src.width) & _mask(rs.width),
                             a)
            if n == "OpFConvert":
                return _map1(lambda x: _rf(x, rs.width), a)
            return self._bitcast(a, src, rs)
        # ----- binary -----
        a = get(o[0][1])
        b = get(o[1][1])
        if n in ("OpIAdd", "OpISub", "OpIMul", "OpUDiv", "OpSDiv", "OpUMod",
                 "OpSRem", "OpSMod", "OpBitwiseAnd", "OpBitwiseOr",
                 "OpBitwiseXor", "OpShiftLeftLogical",
                 "OpShiftRightLogical", "OpShiftRightArithmetic"):
            w = self.scalar(rt).width
            mk = _mask(w)
            if n == "OpIAdd":
                return _map2(lambda x, y: (x + y) & mk, a, b)
            if n == "OpISub":
                return _map2(lambda x, y: (x - y) & mk, a, b)
            if n == "OpIMul":
                return _map2(lambda x, y: (x * y) & mk, a, b)
            if n == "OpBitwiseAnd":
                return _map2(lambda x, y: x & y, a, b)
            if n == "OpBitwiseOr":
                return _map2(lambda x, y: x | y, a, b)
            if n == "OpBitwiseXor":
                return _map2(lambda x, y: x ^ y, a, b)
            if n in ("OpShiftLeftLogical", "OpShiftRightLogical",
                     "OpShiftRightArithmetic"):
                def sh(x, y):
                    if y >= w:
                        raise InterpError("%s: shift by %d of a %d-bit "
                                          "value is undefined" % (n, y, w))
                    if n == "OpShiftLeftLogical":
                        return (x << y) & mk
                    if n == "OpShiftRightLogical":
                        return x >> y
                    return (_sgn(x, w) >> y) & mk
                return _map2(sh, a, b)

            def dv(x, y):
                if y == 0:
                    raise InterpError("%s: division by zero is undefined"
                                      % n)
                if n == "OpUDiv":
                    return x // y
                if n == "OpUMod":
                    return x % y
                sx = _sgn(x, w)
                sy = _sgn(y, w)
                if n == "OpSDiv":
                    q = abs(sx) // abs(sy)
                    if (sx < 0) != (sy < 0):
                        q = -q
                    if q > (1 << (w - 1)) - 1:
                        raise InterpError("OpSDiv overflow is undefined")
                    return q & mk
                r = abs(sx) % abs(sy)
                if n == "OpSRem":
                    if sx < 0:
                        r = -r
                    return r & mk
                r = sx % sy                  # OpSMod: sign of the divisor
                return r & mk
            return _map2(dv, a, b)
        if n in ("OpFAdd", "OpFSub", "OpFMul", "OpFDiv", "OpFRem", "OpFMod"):
            w = self.scalar(rt).width
            fn = {"OpFAdd": _fadd, "OpFSub": lambda x, y: x - y,
                  "OpFMul": _fmul, "OpFDiv": _fdiv, "OpFRem": _frem,
                  "OpFMod": _fmod}[n]
            return _map2(lambda x, y: _rf(fn(x, y), w), a, b)
        if n in ("OpLogicalAnd", "OpLogicalOr", "OpLogicalEqual",
                 "OpLogicalNotEqual"):
            fn = {"OpLogicalAnd": lambda x, y: x and y,
                  "OpLogicalOr": lambda x, y: x or y,
                  "OpLogicalEqual": lambda x, y: x == y,
                  "OpLogicalNotEqual": lambda x, y: x != y}[n]
            return _map2(fn, a, b)
        # ----- comparisons -----
        src = self.scalar(self.rtype[o[0][1]])
        if src.kind == "int":
            w = src.width
            if n == "OpIEqual":
                return _map2(lambda x, y: x == y, a, b)
            if n == "OpINotEqual":
                return _map2(lambda x, y: x != y, a, b)
            tbl = {"OpUGreaterThan": (False, lambda x, y: x > y),
                   "OpSGreaterThan": (True, lambda x, y: x > y),
                   "OpUGreaterThanEqual": (False, lambda x, y: x >= y),
                   "OpSGreaterThanEqual": (True, lambda x, y: x >= y),
                   "OpULessThan": (False, lambda x, y: x < y),
                   "OpSLessThan": (True, lambda x, y: x < y),
                   "OpULessThanEqual": (False, lambda x, y: x <= y),
                   "OpSLessThanEqual": (True, lambda x, y: x <= y)}
            if n in tbl:
                signed = tbl[n][0]
                fn = tbl[n][1]
                if signed:
                    return _map2(lambda x, y: fn(_sgn(x, w), _sgn(y, w)),
                                 a, b)
                return _map2(fn, a, b)
        if src.kind == "float" and n.startswith("OpF"):
            ordered = n.startswith("OpFOrd")
            rel = n[6:] if ordered else n[8:]
            cmp = {"Equal": lambda x, y: x == y,
                   "NotEqual": lambda x, y: x != y,
                   "LessThan": lambda x, y: x < y,
                   "GreaterThan": lambda x, y: x > y,
                   "LessThanEqual": lambda x, y: x <= y,
                   "GreaterThanEqual": lambda x, y: x >= y}[rel]

            def fc(x, y):
                nan = (x != x) or (y != y)
                if nan:
                    return not ordered
                return cmp(x, y)
            return _map2(fc, a, b)
        raise InterpError("unimplemented instruction " + n)

    def _bitcast(self, a, src, dst):
        if src.kind == dst.kind and src.width == dst.width:
            return a

        def one(x):
            if src.kind == "int" and dst.kind == "float":
                fmt = {16: "<H", 32: "<I", 64: "<Q"}[src.width]
                ff = {16: "<e", 32: "<f", 64: "<d"}[dst.width]
                return struct.unpack(ff, struct.pack(fmt, x))[0]
            if src.kind == "float" and dst.kind == "int":
                ff = {16: "<e", 32: "<f", 64: "<d"}[src.width]
                fmt = {16: "<H", 32: "<I", 64: "<Q"}[dst.width]
                return struct.unpack(fmt, struct.pack(ff, x))[0]
            raise InterpError("unsupported bitcast")
        if src.width != dst.width:
            raise InterpError("bitcast between different widths")
        return _map1(one, a)

    def _ext(self, ins, get):
        o = ins.operands
        setname = self.m.ext_imports.get(o[0][1])
        if setname != "GLSL.std.450":
            raise InterpError("unsupported extended instruction set")
        code = o[1][1]
        args = []
        for q in range(2, len(o)):
            args.append(get(o[q][1]))
        rs = self.scalar(ins.type_id)
        if code in (5, 7, 38, 39, 41, 42, 44, 45):     # integer ops
            w = rs.width
            mk = _mask(w)
            if code == 5:
                return _map1(lambda x: abs(_sgn(x, w)) & mk, args[0])
            if code == 7:
                return _map1(lambda x: (_sgn(x, w) > 0) - (_sgn(x, w) < 0)
                             & mk, args[0])
            if code == 38:
                return _map2(min, args[0], args[1])
            if code == 41:
                return _map2(max, args[0], args[1])
            if code == 39:
                return _map2(lambda x, y: x if _sgn(x, w) <= _sgn(y, w)
                             else y, args[0], args[1])
            if code == 42:
                return _map2(lambda x, y: x if _sgn(x, w) >= _sgn(y, w)
                             else y, args[0], args[1])
            if code == 44:
                return _clamp(args, lambda v: v)
            return _clamp(args, lambda v: _sgn(v, w))
        if code not in _GLSL_F:
            raise InterpError("unimplemented GLSL.std.450 instruction %d"
                              % code)
        arity, fn = _GLSL_F[code]
        w = rs.width
        if arity == 1:
            return _map1(lambda x: _rf(fn(x), w), args[0])
        if arity == 2:
            return _map2(lambda x, y: _rf(fn(x, y), w), args[0], args[1])
        return _map3(lambda x, y, z: _rf(fn(x, y, z), w), args[0], args[1],
                     args[2])


def _clamp(args, key):
    x, lo, hi = args[0], args[1], args[2]

    def one(a, b, c):
        v = a
        if key(v) < key(b):
            v = b
        if key(v) > key(c):
            v = c
        return v
    return _map3(one, x, lo, hi)


def _map3(f, a, b, c):
    if isinstance(a, list):
        out = []
        for i in range(len(a)):
            out.append(_map3(f, a[i], b[i], c[i]))
        return out
    return f(a, b, c)


def run(words, entry, grid, buffers, push=b"", validate=True):
    """Run `entry` over `grid` workgroups. `buffers` maps a binding (or a
    (set, binding) pair) to a bytearray, updated in place. With `validate`
    (the default) an invalid module is refused first."""
    if validate:
        errs = rd.validate(words)
        if errs:
            raise InterpError("module does not validate:\n  "
                              + "\n  ".join(errs))
    return Machine(words).run(entry, grid, buffers, push)


def _main(argv):
    if len(argv) < 4:
        print(__doc__)
        return 2
    f = open(argv[1], "rb")
    words = sp.bytes_to_words(f.read())
    f.close()
    grid = tuple(int(x) for x in argv[3].split(","))
    bufs = {}
    files = {}
    for spec in argv[4:]:
        b, path = spec.split("=", 1)
        fh = open(path, "rb")
        bufs[int(b)] = bytearray(fh.read())
        fh.close()
        files[int(b)] = path
    stats = run(words, argv[2], grid, bufs)
    for b in bufs:
        fh = open(files[b], "wb")
        fh.write(bytes(bufs[b]))
        fh.close()
    print("ok: %d steps" % stats["steps"])
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
