"""IL -> SPIR-V lowering (Vulkan compute).  `--target spirv`.

The back end the plan in SPIRV.md describes, Phase 2: scalar kernels over
typed buffers. It is the SPIR-V twin of the wasm back end in asm_gen.py and
makes the same two structural choices, for the same two reasons:

  * No registers. Every IL value gets a Function-storage variable and every
    use is an OpLoad. The driver's compiler promotes them; nothing here
    allocates anything.

  * No goto. Vulkan's `Shader` capability requires structured control flow,
    and the IL is an arbitrary Label/Jump/JumpZero CFG. Exactly as the wasm
    back end did first, the function body is split into basic blocks and run
    from one loop that switches on a `state` variable. That is legal
    structured SPIR-V and correct for any CFG. It is also slow on a GPU (every
    branch is a divergent switch), which is why a real structurizer is the
    next phase and a kernel with no jumps skips the loop entirely.

What a kernel is. A function whose header carries

    assert NAME in gpu.compute(local_size=64)

(parsed by extensions.py). Its pointer parameters are storage buffers, its
scalar parameters one push-constant block, its thread index comes from the
`gpu_global_id(dim)` family declared in <crust_gpu.h>.

Pointers. SPIR-V (Logical addressing) has no pointers, and the IL has already
scaled pointer arithmetic to bytes: `p[i]` is `Add(p, Mult(i, 4))` then
`ReadAt`. So a pointer-typed IL value is lowered to a *byte offset* in a u32
variable, and its buffer is a compile-time fact: every pointer value is
traced back to exactly one pointer parameter, and a value that could refer to
two buffers is refused. The element index is `offset >> log2(sizeof elem)`.

64-bit integers. The front end keeps LP64, so index arithmetic is `long`.
Vulkan's Int64 is optional and absent on some devices, so a program whose
64-bit values only ever feed pointer arithmetic (modular add/sub/mul, which
commute with truncation) is compiled with them held in 32 bits. A program
that needs real 64-bit semantics -- a compare, a divide, a 64-bit parameter --
gets the Int64 capability instead, for the whole module.

Everything not supported refuses with the construct named and the way out,
as the other back ends do. Phase 4 and later lift most of these.
"""

import os

import shivyc.spirv as sp
import shivyc.spirv_reader as rd
import shivyc.spirv_struct as sst
import shivyc.il_cmds.control as control
import shivyc.il_cmds.value as value_cmds
import shivyc.il_cmds.math as math_cmds
import shivyc.il_cmds.compare as compare_cmds
from shivyc.il_gen import IntegerLiteral, FloatLiteral
from shivyc.errors import CompilerError

# <crust_gpu.h> intrinsic -> SPIR-V BuiltIn (a vec3 of u32, indexed by `dim`).
GPU_BUILTINS = {
    "gpu_global_id": "GlobalInvocationId",
    "gpu_local_id": "LocalInvocationId",
    "gpu_group_id": "WorkgroupId",
    "gpu_num_groups": "NumWorkgroups",
}

# gpu_atomic_*(&p[i], ...) -> (SPIR-V opcode for signed, for unsigned, extra
# value operands). `p` is a storage buffer or workgroup array of 32-bit ints.
GPU_ATOMICS = {
    "gpu_atomic_add": ("OpAtomicIAdd", "OpAtomicIAdd", 1),
    "gpu_atomic_sub": ("OpAtomicISub", "OpAtomicISub", 1),
    "gpu_atomic_min": ("OpAtomicSMin", "OpAtomicUMin", 1),
    "gpu_atomic_max": ("OpAtomicSMax", "OpAtomicUMax", 1),
    "gpu_atomic_and": ("OpAtomicAnd", "OpAtomicAnd", 1),
    "gpu_atomic_or": ("OpAtomicOr", "OpAtomicOr", 1),
    "gpu_atomic_xor": ("OpAtomicXor", "OpAtomicXor", 1),
    "gpu_atomic_exchange": ("OpAtomicExchange", "OpAtomicExchange", 1),
    "gpu_atomic_cas": ("OpAtomicCompareExchange",
                       "OpAtomicCompareExchange", 2),
    "gpu_atomic_load": ("OpAtomicLoad", "OpAtomicLoad", 0),
    "gpu_atomic_store": ("OpAtomicStore", "OpAtomicStore", 1),
}

# libm float function -> (GLSL.std.450 instruction, arity).
GPU_MATH = {
    "sqrtf": ("Sqrt", 1), "fabsf": ("FAbs", 1), "floorf": ("Floor", 1),
    "ceilf": ("Ceil", 1), "truncf": ("Trunc", 1), "sinf": ("Sin", 1),
    "cosf": ("Cos", 1), "expf": ("Exp", 1), "logf": ("Log", 1),
    "powf": ("Pow", 2), "fminf": ("FMin", 2), "fmaxf": ("FMax", 2),
    "fmaf": ("Fma", 3),
}

SHARED_LIMIT = 16384      # bytes; Vulkan's guaranteed maxComputeSharedMemorySize

I32 = ("i", 32, True)
U32 = ("i", 32, False)
F32 = ("f", 32)
PTR = ("p",)


def refuse(what, why):
    raise CompilerError("spirv: %s: %s" % (what, why))


def _is_terminator(c):
    return isinstance(c, control.Jump) or isinstance(c, control.JumpZero) \
        or isinstance(c, control.JumpNotZero) \
        or isinstance(c, control.Return)


def _dump_plan(items, depth, out):
    pad = "  " * depth
    for it in items:
        if it[0] == "if":
            out.write("%sif b%d merged=%s\n" % (pad, it[1], it[4]))
            _dump_plan(it[2], depth + 1, out)
            out.write("%selse\n" % pad)
            _dump_plan(it[3], depth + 1, out)
        elif it[0] == "loop":
            out.write("%sloop b%d exits=%s\n" % (pad, it[1], it[3]))
            _dump_plan(it[2], depth + 1, out)
        else:
            out.write("%s%s\n" % (pad, " ".join(str(x) for x in it)))


class _BadModule(CompilerError):
    pass


class SpirvGen:
    def __init__(self, il_code, symbol_table, arguments, structure=None):
        if structure is None:
            structure = os.environ.get("CRUST_SPIRV_STRUCT", "1") != "0"
        self.structure = structure
        self.modes = {}             # fn -> "single" | "structured" | reason
        self.il = il_code
        self.st = symbol_table
        self.arguments = arguments
        self.info = getattr(arguments, "_extensions", None)
        self.m = sp.Module()
        self.native64 = False
        self.shared_vars = {}       # static name -> (var id, kind, count)
        self.shared_bytes = 0
        self.shared_by_name = {}    # static name -> its IL value
        self.has_barrier = False
        self.order = []             # reachable functions, kernels first
        self.kernels = []
        self.calls = {}             # fn -> [callee names]
        self.fobj = {}              # fn -> sp.Function
        self.fn_builtins = {}       # fn -> set of builtin names used
        self.builtin_vars = {}      # builtin name -> variable id
        self.glsl = None
        self.kernel_if = {}         # kernel -> interface record

    # ------------------------------------------------------------ driver

    def make(self):
        out = self._make_with_fallback()
        if os.environ.get("CRUST_SPIRV_REPORT"):
            import sys
            for fn in self.modes:
                sys.stderr.write("spirv: %s: %s\n" % (fn, self.modes[fn]))
        return out

    def _make_with_fallback(self):
        try:
            return self._make()
        except _BadModule as e:
            if not self.structure:
                raise CompilerError(str(e))
            # The structurizer produced something our validator rejects.
            # The dispatcher is correct for any CFG, so use it instead.
            retry = SpirvGen(self.il, self.st, self.arguments, False)
            out = retry.make()
            self.modes = retry.modes
            self.modes["__fallback__"] = str(e)
            return out

    def _make(self):
        self._find_kernels()
        self._collect()
        self._fold_statics()
        self._find_barrier_fns()
        self._decide_int64()
        m = self.m
        m.capability("Shader")
        m.memory_model("Logical", "GLSL450")
        for fn in self.order:
            self._declare(fn)
        for fn in self.order:
            self._lower(fn)
        for fn in self.kernels:
            self._entry_point(fn)
        words = m.words()
        # Never hand out a module our own validator rejects: an emitter bug
        # must show up here, with the rule that broke, not in a driver.
        dump = os.environ.get("CRUST_SPIRV_DUMP")
        if dump and self.structure:
            with open(dump, "wb") as fh:
                fh.write(sp.words_to_bytes(words))
        errs = rd.validate(words)
        if errs:
            shown = errs[:6]
            raise _BadModule(
                "spirv: internal error, the emitted module is invalid:\n  "
                + "\n  ".join(shown))
        return sp.words_to_bytes(words)

    def _find_kernels(self):
        decl = {}
        if self.info is not None:
            decl = self.info.gpu_compute
        for fn in self.il.commands:
            if fn in decl:
                self.kernels.append(fn)
        for fn in decl:
            if fn not in self.il.commands:
                refuse("kernel '%s'" % fn,
                       "declared with gpu.compute but not defined in this "
                       "translation unit")
        if not self.kernels:
            refuse("no kernel",
                   "nothing declares one; put `assert NAME in "
                   "gpu.compute(local_size=N)` between a function's "
                   "parameter list and its body")

    def _collect(self):
        seen = {}
        stack = list(reversed(self.kernels))
        order = []
        while stack:
            fn = stack.pop()
            if fn in seen:
                continue
            seen[fn] = True
            order.append(fn)
            callees = []
            for c in self.il.commands[fn]:
                if not isinstance(c, control.Call):
                    continue
                nm = c.direct_name
                if nm is None:
                    refuse("function '%s'" % fn,
                           "call through a function pointer; a GPU module "
                           "has no code addresses")
                if nm in GPU_BUILTINS or nm in GPU_MATH or \
                        nm in GPU_ATOMICS or nm == "gpu_popcount":
                    continue
                if nm == "gpu_barrier":
                    continue
                if nm not in self.il.commands:
                    refuse("function '%s'" % fn,
                           "call to '%s', which is not defined here; there "
                           "is no libc on a GPU (math: sqrtf fabsf floorf "
                           "ceilf truncf sinf cosf expf logf powf fminf "
                           "fmaxf fmaf)" % nm)
                callees.append(nm)
                stack.append(nm)
            self.calls[fn] = callees
        # kernels first, then helpers, each in discovery order
        for fn in order:
            if fn in self.kernels:
                self.order.append(fn)
        for fn in order:
            if fn not in self.kernels:
                self.order.append(fn)

    # ------------------------------------------------- 64-bit integer policy

    def _is64(self, v):
        if v is None:
            return False
        ct = v.ctype
        return ct.is_integral() and not ct.is_pointer() and ct.size == 8

    def _wide_use(self, c):
        """Reason `c` needs genuine 64-bit integer semantics, or None. Only
        modular add/sub/mul/and/or/xor/neg/not, plain copies and truncation
        survive being computed on the low 32 bits."""
        if isinstance(c, value_cmds.LoadArg):
            if self._is64(c.output):
                return "a 64-bit parameter"
        elif isinstance(c, value_cmds.ReadAt):
            if self._is64(c.output):
                return "a 64-bit load"
        elif isinstance(c, value_cmds.SetAt):
            if self._is64(c.val):
                return "a 64-bit store"
        elif isinstance(c, value_cmds.Set):
            if self._is64(c.output):
                lit = c.arg.literal
                if c.arg.ctype.is_floating():
                    return "a float to 64-bit conversion"
                if lit is not None and isinstance(lit, IntegerLiteral):
                    if lit.val >= (1 << 32) or lit.val < -(1 << 31):
                        return "a 64-bit constant"
            elif self._is64(c.arg) and c.output.ctype.is_floating():
                return "a 64-bit to float conversion"
        elif isinstance(c, math_cmds.Div) or isinstance(c, math_cmds.Mod) \
                or isinstance(c, math_cmds.RBitShift) \
                or isinstance(c, math_cmds.LBitShift):
            if self._is64(c.arg1) or self._is64(c.arg2):
                return "a 64-bit divide, remainder or shift"
        elif isinstance(c, compare_cmds._GeneralCmp):
            if self._is64(c.arg1) or self._is64(c.arg2):
                return "a 64-bit comparison"
        elif isinstance(c, control.Return):
            if c.arg is not None and self._is64(c.arg):
                return "a 64-bit return value"
        elif isinstance(c, control.Call):
            for a in c.args:
                if self._is64(a):
                    return "a 64-bit call argument"
            if c.ret is not None and self._is64(c.ret):
                return "a 64-bit call result"
        return None

    def _fold_statics(self):
        """A file-scope scalar nothing ever writes (Rust `const X: f32 =
        1.5;`, C `static const float X = 1.5f;`) is just a constant: give it
        a literal so it needs no storage. Anything that is written, has its
        address taken, or is not a plain number keeps its STATIC storage and
        is refused later, with the usual message."""
        STATIC = self.st.STATIC
        written = {}
        addressed = {}
        for fn in self.order:
            for c in self.il.commands[fn]:
                for v in c.outputs():
                    if v is not None:
                        written[v] = True
                if isinstance(c, value_cmds.AddrOf):
                    for v in c.inputs():
                        if v is not None:
                            addressed[v] = True
        for fn in self.order:
            for c in self.il.commands[fn]:
                for v in c.inputs():
                    if v is None or v.literal is not None:
                        continue
                    if self.st.storage.get(v) != STATIC:
                        continue
                    if v in written or v in addressed:
                        continue
                    if self._is_shared(v):
                        continue
                    if v in self.il.static_block_inits:
                        continue
                    init = self.il.static_inits.get(v, 0)
                    if isinstance(init, bool) or not isinstance(
                            init, (int, float)):
                        continue
                    if v.ctype.is_floating() and v.ctype.size == 4:
                        v.literal = FloatLiteral(init)
                    elif v.ctype.is_integral() and not v.ctype.is_bool():
                        v.literal = IntegerLiteral(init)

    # ----------------------------------------------- workgroup memory

    def _is_shared(self, v):
        """A file-scope static the kernels declared `gpu.shared`."""
        if v is None or v.literal is not None or self.info is None:
            return False
        if self.st.storage.get(v) != self.st.STATIC:
            return False
        return self.st.names.get(v) in self.info.gpu_shared

    def _shared_var(self, v, where):
        """The module-level Workgroup variable for the shared static `v`:
        (id, element kind, count or None for a scalar). Made on first use."""
        name = self.st.names[v]
        if name in self.shared_vars:
            return self.shared_vars[name]
        m = self.m
        ct = v.ctype
        if ct.is_array():
            if ct.n is None or ct.n < 1:
                refuse(where, "shared array '%s' needs a fixed length" % name)
            el = ct.el
            if el.is_array() or el.is_struct_union() or el.is_pointer():
                refuse(where, "shared '%s': only one-dimensional arrays of "
                              "numbers are supported (structs and "
                              "multi-dimensional arrays are SPIRV.md phase 4)"
                       % name)
            ek = self.kind(el, where)
            count = ct.n
            ty = m.t_array(self.tid(ek),
                           m.const_int(m.t_int(32, False), count))
            size = count * 4
        elif ct.is_pointer() or ct.is_struct_union():
            refuse(where, "shared '%s': pointers and structs cannot live in "
                          "workgroup memory" % name)
        else:
            ek = self.kind(ct, where)
            count = None
            ty = self.tid(ek)
            size = 4
        self.shared_bytes += size
        if self.shared_bytes > SHARED_LIMIT:
            refuse(where, "workgroup memory is %d bytes; Vulkan only "
                          "guarantees %d" % (self.shared_bytes, SHARED_LIMIT))
        var = m.global_var(m.t_pointer("Workgroup", ty), "Workgroup")
        m.name(var, name)
        rec = (var, ek, count)
        self.shared_vars[name] = rec
        return rec

    def _shared_elem(self, base, count, chunk, out_kind, where):
        """Pointer to element `count` (plus a constant byte `chunk`) of the
        shared array `base`."""
        m = self.m
        b = self.b
        var, ek, n = self._shared_var(base, where)
        if n is None:
            refuse(where, "indexing the shared scalar '%s'"
                   % self.st.names[base])
        if ek[0] != out_kind[0] or ek[1] != out_kind[1]:
            refuse(where, "accessing shared array '%s' as a different type"
                   % self.st.names[base])
        u32 = m.t_int(32, False)
        # IL convention: with a count the byte offset is count * chunk (chunk
        # is the element size); without one it is just chunk.
        if count is not None:
            if chunk != 4:
                refuse(where, "accessing shared '%s' with a different "
                              "element size" % self.st.names[base])
            idx = self.ld_as(count, U32)
        else:
            if chunk % 4:
                refuse(where, "a misaligned access to shared '%s'"
                       % self.st.names[base])
            idx = m.const_int(u32, chunk // 4)
        return b.op("OpAccessChain", m.t_pointer("Workgroup", self.tid(ek)),
                    var, idx), ek

    def _addr_rel(self, c):
        """&shared[i]: the pointer is a byte offset into the array."""
        m = self.m
        u32 = m.t_int(32, False)
        if c.count is not None:
            off = self.b.op("OpIMul", u32, self.ld_as(c.count, U32),
                            m.const_int(u32, c.chunk))
        else:
            off = m.const_int(u32, c.chunk)
        self.b.store(self.var[c.output], off)

    def _rel_read(self, c):
        where = "function '%s'" % self.fn
        ok = self.vkind[c.output]
        p, ek = self._shared_elem(c.base, c.count, c.chunk, ok, where)
        self.st_(c.output, self.b.load(self.tid(ek), p))

    def _rel_set(self, c):
        where = "function '%s'" % self.fn
        vk = self.kind(c.val.ctype, where)
        p, ek = self._shared_elem(c.base, c.count, c.chunk, vk, where)
        self.b.store(p, self.ld_as(c.val, ek))

    def _atomic(self, c, nm):
        m = self.m
        b = self.b
        where = "function '%s'" % self.fn
        sop, uop, extra = GPU_ATOMICS[nm]
        if len(c.args) != 1 + extra:
            refuse(where, "%s takes a pointer and %d value(s)"
                   % (nm, extra))
        key = self.base_of[c.args[0]]
        buf = self.bufs[key]
        ek = buf["kind"]
        if ek[0] != "i" or ek[1] != 32:
            refuse(where, "%s on '%s': atomics need a 32-bit integer "
                          "element" % (nm, buf["name"]))
        p, _ = self._elem_access(c.args[0], ek, where)
        u32 = m.t_int(32, False)
        shared = buf.get("storage") == "Workgroup"
        scope = m.const_int(u32, sp.SCOPE["Workgroup" if shared
                                          else "Device"])
        sem = m.const_int(u32, 0)
        op = sop if ek[2] else uop
        t = self.tid(ek)
        vals = [self.ld_as(a, ek) for a in c.args[1:]]
        if op == "OpAtomicStore":
            b.op(op, 0, p, scope, sem, vals[0])
            return
        if op == "OpAtomicLoad":
            r = b.op(op, t, p, scope, sem)
        elif op == "OpAtomicCompareExchange":
            # gpu_atomic_cas(p, expected, desired) -> old value
            r = b.op(op, t, p, scope, sem, sem, vals[1], vals[0])
        else:
            r = b.op(op, t, p, scope, sem, vals[0])
        if c.ret is not None:
            self.st_(c.ret, self._conv(r, ek, self.vkind[c.ret]))

    def _barrier(self):
        m = self.m
        u32 = m.t_int(32, False)
        wg = m.const_int(u32, sp.SCOPE["Workgroup"])
        sem = m.const_int(u32, sp.SEM_ACQUIRE_RELEASE
                          | sp.SEM_WORKGROUP_MEMORY)
        self.b.op("OpControlBarrier", 0, wg, wg, sem)

    def _find_barrier_fns(self):
        """Functions that reach a gpu_barrier(), directly or through calls.
        A call to one counts as a barrier at its call site."""
        self.barrier_fns = set()
        for fn in self.order:
            for c in self.il.commands[fn]:
                if isinstance(c, control.Call) and \
                        c.direct_name == "gpu_barrier":
                    self.barrier_fns.add(fn)
        changed = True
        while changed:
            changed = False
            for fn in self.order:
                if fn in self.barrier_fns:
                    continue
                for callee in self.calls.get(fn, []):
                    if callee in self.barrier_fns:
                        self.barrier_fns.add(fn)
                        changed = True
                        break

    def _is_barrier_call(self, c):
        return isinstance(c, control.Call) and (
            c.direct_name == "gpu_barrier" or c.direct_name in self.barrier_fns)

    def _check_barriers(self, fn, blocks, label_block):
        """Refuse a gpu_barrier() that not every invocation of the workgroup
        is certain to reach. Vulkan makes that undefined (a hang on some
        drivers), so it is a compile error here, not a surprise later.

        A barrier in block B is safe when B post-dominates every branch whose
        condition can differ between invocations: whichever way such a branch
        goes, the invocation still arrives at B. A condition is uniform when it
        derives only from literals, push constants and workgroup ids; thread
        ids, buffer loads and workgroup-memory reads count as divergent.
        Returns whether the function has a barrier."""
        where = "function '%s'" % fn
        bar = []
        for i, blk in enumerate(blocks):
            for c in blk:
                if self._is_barrier_call(c):
                    bar.append(i)
                    break
        if not bar:
            return False
        div = set()
        cmds = [c for blk in blocks for c in blk]
        changed = True
        while changed:
            changed = False
            for c in cmds:
                bad = False
                if isinstance(c, control.Call):
                    nm = c.direct_name
                    bad = nm not in ("gpu_group_id", "gpu_num_groups") \
                        and nm != "gpu_barrier"
                elif isinstance(c, value_cmds.LoadArg):
                    # a kernel's scalars are push constants (uniform); a
                    # helper's arguments could be anything
                    bad = fn not in self.kernels
                elif isinstance(c, (value_cmds.ReadAt, value_cmds.ReadRel)):
                    bad = True
                else:
                    for v in c.inputs():
                        if v is not None and (v in div or self._is_shared(v)):
                            bad = True
                if bad:
                    for v in c.outputs():
                        if v is not None and v not in div:
                            div.add(v)
                            changed = True
        n = len(blocks)

        def succs(i):
            last = blocks[i][-1] if blocks[i] else None
            if isinstance(last, control.Return):
                return []
            if isinstance(last, control.Jump):
                return [label_block[last.label]]
            nxt = [i + 1] if i + 1 < n else []
            if isinstance(last, (control.JumpZero, control.JumpNotZero)):
                return [label_block[last.label]] + nxt
            return nxt

        # post-dominator sets with a virtual exit node n
        S = [succs(i) for i in range(n)]
        full = set(range(n + 1))
        pdom = [set(full) for _ in range(n)] + [{n}]
        for i in range(n):
            if not S[i]:
                S[i] = [n]
        changed = True
        while changed:
            changed = False
            for i in range(n):
                new = set(full)
                for j in S[i]:
                    new &= pdom[j]
                new = new | {i}
                if new != pdom[i]:
                    pdom[i] = new
                    changed = True

        for i in range(n):
            last = blocks[i][-1] if blocks[i] else None
            if not isinstance(last, (control.JumpZero, control.JumpNotZero)):
                continue
            if last.cond not in div:
                continue
            for bblk in bar:
                # B is control dependent on this branch: some successor is
                # certain to arrive at B, yet the branch itself is not.
                if bblk not in pdom[i] and any(
                        bblk in pdom[y] for y in S[i] if y != n):
                    refuse(where, "gpu_barrier() sits under a condition "
                                  "that can differ between invocations (a "
                                  "thread-id test, a load, an early return "
                                  "or a loop whose trip count varies); "
                                  "every invocation of a workgroup must "
                                  "reach it. Do the divergent part inside "
                                  "the barriers, not around them")
        return True

    def _decide_int64(self):
        for fn in self.order:
            for c in self.il.commands[fn]:
                why = self._wide_use(c)
                if why is not None:
                    self.native64 = True
                    self.wide_reason = "function '%s' has %s" % (fn, why)
                    return
        self.wide_reason = None

    # ------------------------------------------------------------- types

    def kind(self, ctype, where):
        """('i', storage width, signed) | ('f', 32) | ('p',) for a C type."""
        if ctype.is_pointer():
            return PTR
        if ctype.is_floating():
            if ctype.size != 4:
                refuse(where, "double needs the Float64 capability, which is "
                              "not lowered yet; use float")
            return F32
        if ctype.is_integral():
            if ctype.is_bool():
                # A bool is a 32-bit 0 or 1; Set and Return normalize what is
                # stored into one (see _truth).
                return U32
            if ctype.size == 1 or ctype.size == 2:
                refuse(where, "8- and 16-bit integers (char, short) need the "
                              "Int8/Int16 capabilities, which are not "
                              "lowered yet; use int")
            w = 32
            if ctype.size == 8 and self.native64:
                w = 64
            return ("i", w, bool(ctype.signed))
        refuse(where, "a value of this type (array, struct or void) cannot "
                      "live in a register-like variable")

    def tid(self, k):
        if k[0] == "i":
            return self.m.t_int(k[1], False)
        if k[0] == "f":
            return self.m.t_float(32)
        return self.m.t_int(32, False)       # a pointer is a u32 byte offset

    # --------------------------------------------------- signatures / decls

    def _sig(self, fn):
        var = self.st.linkages[self.st.EXTERNAL].get(fn)
        if var is None:
            var = self.st.linkages[self.st.INTERNAL].get(fn)
        if var is None or getattr(var, "ctype", None) is None:
            refuse("function '%s'" % fn, "no signature in the symbol table")
        return var.ctype

    def _declare(self, fn):
        m = self.m
        ct = self._sig(fn)
        where = "function '%s'" % fn
        if getattr(ct, "variadic", False):
            refuse(where, "variadic functions are not supported on the GPU")
        if fn in self.kernels:
            if not ct.ret.is_void():
                refuse(where, "a kernel must return void; write results "
                              "through a buffer parameter")
            self.fobj[fn] = m.function(m.t_void())
            m.name(self.fobj[fn].id, fn)
            return
        ptypes = []
        for a in ct.args:
            if a.is_pointer():
                refuse(where, "a helper function cannot take a pointer "
                              "parameter yet (it would need inlining into "
                              "the kernel so the buffer is known; SPIRV.md "
                              "phase 3); pass an index, or inline it by hand")
            if a.is_array() or a.is_struct_union():
                refuse(where, "aggregate parameters are not supported yet")
            ptypes.append(self.tid(self.kind(a, where)))
        if ct.ret.is_void():
            rt = m.t_void()
        else:
            rt = self.tid(self.kind(ct.ret, where))
        self.fobj[fn] = m.function(rt, ptypes)
        m.name(self.fobj[fn].id, fn)

    # ------------------------------------------------- pointer provenance

    def _pointer_bases(self, fn, cmds):
        """Trace every pointer-typed IL value back to one pointer parameter.

        Returns (base_of, reads, writes): value -> parameter index, and the
        sets of parameter indices the function loads from / stores through.
        A value reachable from two different parameters is refused: it would
        have to name two buffers at once.
        """
        parent = {}
        seed = {}

        def find(v):
            while parent.get(v, v) is not v:
                v = parent[v]
            return v

        def union(a, b):
            ra = find(a)
            rb = find(b)
            if ra is rb:
                return
            parent[rb] = ra
            if rb in seed:
                if ra in seed and seed[ra] != seed[rb]:
                    refuse("function '%s'" % fn,
                           "a pointer may refer to buffer parameter %d or "
                           "%d; a pointer must resolve to a single buffer "
                           "at compile time" % (seed[ra], seed[rb]))
                seed[ra] = seed[rb]

        for c in cmds:
            if isinstance(c, value_cmds.LoadArg) and \
                    c.output.ctype.is_pointer():
                parent[c.output] = c.output
                seed[c.output] = c.arg_num
            elif isinstance(c, (value_cmds.AddrOf, value_cmds.AddrRel)) and \
                    self._is_shared(c.base if isinstance(
                        c, value_cmds.AddrRel) else c.var):
                # &shared[0] / &shared_scalar: a pointer into workgroup
                # memory, keyed ("sh", name) where a parameter has its index
                sv = c.base if isinstance(c, value_cmds.AddrRel) else c.var
                nm = self.st.names[sv]
                self.shared_by_name[nm] = sv
                parent[c.output] = c.output
                seed[c.output] = ("sh", nm)
        for c in cmds:
            if isinstance(c, value_cmds.Set) and \
                    c.output.ctype.is_pointer():
                if c.arg.literal is not None or not c.arg.ctype.is_pointer():
                    refuse("function '%s'" % fn,
                           "a pointer set from a constant or a non-pointer "
                           "(a null or integer-valued pointer)")
                union(c.output, c.arg)
            elif isinstance(c, math_cmds.Add) or \
                    isinstance(c, math_cmds.Subtr):
                if c.output.ctype.is_pointer():
                    pa = c.arg1.ctype.is_pointer()
                    pb = c.arg2.ctype.is_pointer()
                    if pa and pb:
                        refuse("function '%s'" % fn,
                               "pointer minus pointer is not supported")
                    if not pa and not pb:
                        refuse("function '%s'" % fn,
                               "pointer arithmetic on a non-pointer")
                    if isinstance(c, math_cmds.Subtr) and pb:
                        refuse("function '%s'" % fn,
                               "integer minus pointer")
                    union(c.output, c.arg1 if pa else c.arg2)
        base_of = {}
        reads = {}
        writes = {}
        vals = []
        for c in cmds:
            for v in c.inputs() + c.outputs():
                if v is not None and v.literal is None and \
                        v.ctype.is_pointer():
                    vals.append(v)
        for v in vals:
            r = find(v)
            if r in seed:
                base_of[v] = seed[r]
        for c in cmds:
            if isinstance(c, value_cmds.ReadAt):
                if c.addr not in base_of:
                    refuse("function '%s'" % fn,
                           "a load through a pointer whose buffer is not "
                           "known at compile time")
                reads[base_of[c.addr]] = True
            elif isinstance(c, value_cmds.SetAt):
                if c.addr not in base_of:
                    refuse("function '%s'" % fn,
                           "a store through a pointer whose buffer is not "
                           "known at compile time")
                writes[base_of[c.addr]] = True
            elif isinstance(c, control.Call) and \
                    c.direct_name in GPU_ATOMICS:
                if not c.args or c.args[0] not in base_of:
                    refuse("function '%s'" % fn,
                           "%s needs a pointer into a buffer or a "
                           "workgroup array, as in gpu_atomic_add(&a[i], 1)"
                           % c.direct_name)
                key = base_of[c.args[0]]
                if c.direct_name != "gpu_atomic_load":
                    writes[key] = True
                if c.direct_name != "gpu_atomic_store":
                    reads[key] = True
        return base_of, reads, writes

    # -------------------------------------------------- kernel interface

    def _kernel_interface(self, fn, cmds, reads, writes):
        """Declare the storage buffers and the push-constant block."""
        m = self.m
        ct = self._sig(fn)
        where = "kernel '%s'" % fn
        names = {}
        for c in cmds:
            if isinstance(c, value_cmds.LoadArg):
                nm = self.st.names.get(c.output)
                if nm is not None:
                    names[c.arg_num] = nm
        declared = {}
        if self.info is not None:
            declared = self.info.gpu_buffers.get(fn, {})
        explicit = {}
        for nm in declared:
            explicit[declared[nm]["binding"]] = True
        for nm in declared:
            if nm not in names.values():
                refuse(where, "gpu.buffer names '%s', which is not a "
                              "parameter" % nm)
        nxt = 0
        buffers = {}
        scalars = []                      # (arg index, kind, name)
        for k in range(len(ct.args)):
            a = ct.args[k]
            pname = names.get(k, "arg%d" % k)
            if a.is_pointer():
                ek = self.kind(a.arg, "%s, parameter '%s'" % (where, pname))
                if ek[0] == "p":
                    refuse(where, "parameter '%s' is a pointer to a "
                                  "pointer" % pname)
                if ek[1 if ek[0] == "i" else 1] != 32:
                    refuse(where, "buffer element of 64 bits (parameter "
                                  "'%s') is not lowered yet" % pname)
                rec = declared.get(pname)
                if rec is not None:
                    binding = rec["binding"]
                    dset = rec["set"]
                    readonly = rec["readonly"]
                    writeonly = rec["writeonly"]
                else:
                    while nxt in explicit:
                        nxt = nxt + 1
                    binding = nxt
                    explicit[binding] = True
                    dset = 0
                    readonly = False
                    writeonly = False
                if readonly and k in writes:
                    refuse(where, "'%s' is declared readonly but the "
                                  "kernel stores through it" % pname)
                if writeonly and k in reads:
                    refuse(where, "'%s' is declared writeonly but the "
                                  "kernel loads from it" % pname)
                if k not in writes:
                    readonly = True       # inferred: never written
                et = self.tid(ek)
                rt = m.t_runtime_array(et)
                m.decorate(rt, "ArrayStride", 4)
                stt = m.t_struct([rt])
                m.decorate(stt, "Block")
                m.member_decorate(stt, 0, "Offset", 0)
                if readonly:
                    m.member_decorate(stt, 0, "NonWritable")
                if writeonly:
                    m.member_decorate(stt, 0, "NonReadable")
                var = m.global_var(m.t_pointer("StorageBuffer", stt),
                                   "StorageBuffer")
                m.decorate(var, "DescriptorSet", dset)
                m.decorate(var, "Binding", binding)
                m.name(var, pname)
                m.name(stt, pname + "_buffer")
                buffers[k] = {"var": var, "kind": ek, "name": pname,
                              "binding": binding}
            else:
                if a.is_array() or a.is_struct_union():
                    refuse(where, "aggregate parameter '%s'" % pname)
                sk = self.kind(a, "%s, parameter '%s'" % (where, pname))
                if sk[0] == "i" and sk[1] != 32:
                    refuse(where, "64-bit scalar parameter '%s'" % pname)
                scalars.append((k, sk, pname))
        push = None
        if scalars:
            members = []
            for s in scalars:
                members.append(self.tid(s[1]))
            stt = m.t_struct(members)
            m.decorate(stt, "Block")
            m.name(stt, fn + "_push")
            index = {}
            for i in range(len(scalars)):
                m.member_decorate(stt, i, "Offset", 4 * i)
                m.member_name(stt, i, scalars[i][2])
                index[scalars[i][0]] = i
            var = m.global_var(m.t_pointer("PushConstant", stt),
                               "PushConstant")
            m.name(var, fn + "_args")
            push = {"var": var, "index": index, "scalars": scalars}
        rec = {"buffers": buffers, "push": push}
        self.kernel_if[fn] = rec
        return rec

    # -------------------------------------------------------- block split

    def _split(self, cmds):
        blocks = [[]]
        label_block = {}
        for c in cmds:
            if isinstance(c, control.Label):
                if blocks[-1]:
                    blocks.append([])
                label_block[c.label] = len(blocks) - 1
                continue
            blocks[-1].append(c)
            if _is_terminator(c):
                blocks.append([])
        if len(blocks) > 1 and not blocks[-1]:
            last = len(blocks) - 1
            pointed = False
            for lbl in label_block:
                if label_block[lbl] == last:
                    pointed = True
            if not pointed:
                blocks.pop()
        return blocks, label_block

    # ---------------------------------------------------------- lowering

    def _lower(self, fn):
        m = self.m
        cmds = self.il.commands[fn]
        f = self.fobj[fn]
        self.fn = fn
        self.f = f
        self.is_kernel = fn in self.kernels
        self.fn_builtins[fn] = {}
        where = "function '%s'" % fn
        STATIC = self.st.STATIC

        for c in cmds:
            for v in c.inputs() + c.outputs():
                if v is None:
                    continue
                if v.literal is not None:
                    if not isinstance(v.literal, IntegerLiteral) and \
                            not isinstance(v.literal, FloatLiteral):
                        refuse(where, "string literals are not supported "
                                      "on the GPU")
                    continue
                if self.st.storage.get(v) == STATIC and \
                        not self._is_shared(v):
                    refuse(where, "a global or static variable; kernels "
                                  "have no static storage -- declare it "
                                  "`assert NAME in gpu.shared` for "
                                  "workgroup memory")
            if isinstance(c, value_cmds.AddrOf) and \
                    not self._is_shared(c.var):
                refuse(where, "taking the address of a variable (local "
                              "arrays and structs are SPIRV.md phase 4)")
            if isinstance(c, value_cmds._RelCommand) and not (
                    isinstance(c, (value_cmds.ReadRel, value_cmds.SetRel,
                                   value_cmds.AddrRel))
                    and self._is_shared(c.base)):
                refuse(where, "struct member access (structs are SPIRV.md "
                              "phase 4)")
            if isinstance(c, value_cmds.LoadStructArg) or \
                    isinstance(c, value_cmds.UnpackArgs):
                refuse(where, "struct parameters are not supported")
            if isinstance(c, value_cmds.VaSaveBase) or \
                    isinstance(c, value_cmds.VaStartAddr):
                refuse(where, "variadic functions are not supported")
        for c in cmds:
            if type(c).__name__ == "InlineAsm":
                refuse(where, "inline assembly")

        base_of, reads, writes = self._pointer_bases(fn, cmds)
        self.base_of = base_of
        self.bufs = {}
        self.push = None
        if self.is_kernel:
            rec = self._kernel_interface(fn, cmds, reads, writes)
            self.bufs = rec["buffers"]
            self.push = rec["push"]
        else:
            for k in set(base_of.values()):
                if not isinstance(k, tuple):
                    refuse(where, "pointer values in a helper function")
        for k in set(base_of.values()):
            if isinstance(k, tuple):
                var, ek, count = self._shared_var(self.shared_by_name[k[1]],
                                                  where)
                self.bufs[k] = {"var": var, "kind": ek, "name": k[1],
                                "storage": "Workgroup", "count": count}

        blocks, label_block = self._split(cmds)
        single = len(blocks) == 1
        self.has_barrier = self._check_barriers(fn, blocks, label_block)

        # One Function variable per IL value.
        entry = f.block()
        self.var = {}
        self.vkind = {}
        for c in cmds:
            for v in c.inputs() + c.outputs():
                if v is None or v.literal is not None or v in self.var:
                    continue
                if self._is_shared(v):
                    var, ek, count = self._shared_var(v, where)
                    if count is None:
                        self.vkind[v] = ek
                        self.var[v] = var
                    continue
                k = self.kind(v.ctype, where)
                self.vkind[v] = k
                self.var[v] = f.local_var(
                    m.t_pointer("Function", self.tid(k)))
        u32 = m.t_int(32, False)
        self.c0 = m.const_int(u32, 0)
        self.c1 = m.const_int(u32, 1)

        if single:
            self.modes[fn] = "single"
            self.b = entry
            self.state = None
            self._emit_block(blocks[0], 0, 1, label_block, True)
            if not entry.terminated:
                self._implicit_return(entry)
            return

        if self.structure:
            plan, why = self._plan(blocks, label_block)
            if plan is not None:
                self.modes[fn] = "structured"
                self._lower_structured(entry, blocks, label_block, plan)
                return
        if self.has_barrier:
            refuse(where, "gpu_barrier() needs structured control flow, "
                          "which this function's branches do not reduce to"
                          + (" (%s)" % why if self.structure and why else
                             " (CRUST_SPIRV_STRUCT=0 forces the dispatcher "
                             "loop)"))
            self.modes[fn] = "dispatcher: " + why
        else:
            self.modes[fn] = "dispatcher: disabled"

        # The dispatcher.
        self.state = f.local_var(m.t_pointer("Function", u32))
        entry.store(self.state, self.c0)
        header = f.block()
        dispatch = f.block()
        cases = []
        for _i in range(len(blocks)):
            cases.append(f.block())
        sw_merge = f.block()
        cont = f.block()
        merge = f.block()
        entry.branch(header.label)
        header.loop_merge(merge.label, cont.label)
        header.branch(dispatch.label)
        sv = dispatch.load(u32, self.state)
        dispatch.selection_merge(sw_merge.label)
        pairs = []
        for i in range(len(cases)):
            pairs.append(i)
            pairs.append(cases[i].label)
        dispatch.op("OpSwitch", 0, sv, sw_merge.label, *pairs)
        self.sw_merge = sw_merge
        for i in range(len(blocks)):
            self.b = cases[i]
            self._emit_block(blocks[i], i, len(blocks), label_block, False)
            if not self.b.terminated:
                self._goto_or_return(i + 1, len(blocks))
        sw_merge.branch(cont.label)
        cont.branch(header.label)
        merge.op("OpUnreachable", 0)

    # ------------------------------------------------ structured lowering

    def _plan(self, blocks, label_block):
        """Ask the structurizer for a tree. Returns (plan, reason)."""
        n = len(blocks)
        kinds = []
        succs = []
        virtual = False
        for i in range(n):
            cmds = blocks[i]
            last = cmds[-1] if cmds else None
            nxt = i + 1
            if nxt >= n:
                nxt = n
                virtual = True
            if isinstance(last, control.Return):
                kinds.append("ret")
                succs.append([])
            elif isinstance(last, control.Jump):
                kinds.append("jmp")
                succs.append([label_block[last.label]])
            elif isinstance(last, control.JumpZero) or \
                    isinstance(last, control.JumpNotZero):
                if label_block[last.label] == nxt:
                    # `if (c) {}`: both ways lead to the next block.
                    kinds.append("fall")
                    succs.append([nxt])
                else:
                    kinds.append("cond")
                    succs.append([label_block[last.label], nxt])
            else:
                kinds.append("fall")
                succs.append([nxt])
        if virtual:
            kinds.append("ret")
            succs.append([])
        if os.environ.get("CRUST_SPIRV_REPORT") == "2":
            import sys
            for i in range(len(kinds)):
                sys.stderr.write("  %s b%d %s %s\n" % (self.fn, i, kinds[i],
                                                       succs[i]))
        sizes = []
        for i in range(n):
            sizes.append(len(blocks[i]))
        if virtual:
            sizes.append(0)
        try:
            return sst.structurize(kinds, succs, sizes), ""
        except sst.Unstructurable as e:
            return None, str(e)

    def _lower_structured(self, entry, blocks, label_block, plan):
        f = self.f
        if os.environ.get("CRUST_SPIRV_REPORT") == "3":
            import sys
            _dump_plan(plan, 0, sys.stderr)
        self.st_blocks = blocks
        self.st_loops = []          # (continue block, merge block)
        self.st_merges = []         # merge blocks of enclosing ifs
        first = f.block()
        entry.branch(first.label)
        self.b = first
        self._st_seq(plan)
        if not self.b.terminated:
            raise CompilerError("spirv: internal error, structured code "
                                "falls off the end of function '%s'"
                                % self.fn)

    def _st_seq(self, items):
        for it in items:
            self._st_item(it)

    def _st_item(self, it):
        f = self.f
        kind = it[0]
        if kind == "block":
            i = it[1]
            if i < len(self.st_blocks):
                for c in self.st_blocks[i]:
                    if not _is_terminator(c):
                        self._emit(c, i, 0, None, False)
        elif kind == "ret":
            i = it[1]
            last = None
            if i < len(self.st_blocks) and self.st_blocks[i]:
                last = self.st_blocks[i][-1]
            if isinstance(last, control.Return):
                self._return(last)
            else:
                self._implicit_return(self.b)
        elif kind == "br":
            if it[1] == "break":
                self.b.branch(self.st_loops[-1][1].label)
            elif it[1] == "continue":
                self.b.branch(self.st_loops[-1][0].label)
            else:
                self.b.branch(self.st_merges[-1].label)
        elif kind == "if":
            c = self.st_blocks[it[1]][-1]
            taken = self._cond_taken(c)
            merge = f.new_block()
            tb = f.block()
            eb = f.new_block()
            self.b.selection_merge(merge.label)
            self.b.cond_branch(taken, tb.label, eb.label)
            self.st_merges.append(merge)
            self.b = tb
            self._st_seq(it[2])
            if not self.b.terminated:
                self.b.branch(merge.label)
            f.place(eb)
            self.b = eb
            self._st_seq(it[3])
            if not self.b.terminated:
                self.b.branch(merge.label)
            self.st_merges.pop()
            f.place(merge)
            self.b = merge
            if not it[4]:
                merge.op("OpUnreachable", 0)
        elif kind == "loop":
            hdr = f.block()
            self.b.branch(hdr.label)
            cont = f.new_block()
            merge = f.new_block()
            hdr.loop_merge(merge.label, cont.label)
            body = f.block()
            hdr.branch(body.label)
            self.b = body
            self.st_loops.append((cont, merge))
            self._st_seq(it[2])
            if not self.b.terminated:
                self.b.branch(cont.label)
            self.st_loops.pop()
            f.place(cont)
            cont.branch(hdr.label)
            f.place(merge)
            self.b = merge
            if not it[3]:
                merge.op("OpUnreachable", 0)
        else:
            raise CompilerError("spirv: internal error, unknown plan item %r"
                                % (kind,))

    def _cond_taken(self, c):
        """Bool id: true when the conditional jump c is taken."""
        m = self.m
        k = self.kind(c.cond.ctype, "function '%s'" % self.fn)
        if k[0] != "i":
            refuse("function '%s'" % self.fn,
                   "a branch on a non-integer condition")
        v = self.ld_as(c.cond, k)
        if isinstance(c, control.JumpZero):
            return self.b.op("OpIEqual", m.t_bool(), v, self._const(k, 0))
        return self.b.op("OpINotEqual", m.t_bool(), v, self._const(k, 0))

    def _implicit_return(self, blk):
        """Falling off the end of the function."""
        if self.is_kernel or self._sig(self.fn).ret.is_void():
            blk.ret()
        else:
            k = self.kind(self._sig(self.fn).ret, "function '%s'" % self.fn)
            blk.op("OpReturnValue", 0, self._const(k, 0))

    def _goto_or_return(self, idx, total):
        if idx >= total:
            self._implicit_return(self.b)
        else:
            self._goto(idx)

    def _goto(self, idx):
        u32 = self.m.t_int(32, False)
        self.b.store(self.state, self.m.const_int(u32, idx))
        self.b.branch(self.sw_merge.label)

    # ------------------------------------------------------- value helpers

    def _const(self, k, val):
        m = self.m
        if k[0] == "f":
            return m.const_float(m.t_float(32), float(val))
        w = k[1]
        mask = (1 << w) - 1
        return m.const_int(m.t_int(w, False), int(val) & mask, w)

    def _lit_value(self, v, dst):
        lit = v.literal
        val = lit.val
        if dst[0] == "f":
            return self._const(dst, float(val))
        if dst[0] == "p":
            refuse("function '%s'" % self.fn,
                   "a pointer from a constant (null pointers do not exist "
                   "on the GPU)")
        if isinstance(val, float):
            val = int(val)
        return self._const(dst, val)

    def _truth(self, val, k):
        """C's conversion to _Bool: 1 if val is not zero, else 0."""
        m = self.m
        b = self.b
        if k[0] == "i":
            nz = b.op("OpINotEqual", m.t_bool(), val, self._const(k, 0))
        elif k[0] == "f":
            nz = b.op("OpFUnordNotEqual", m.t_bool(), val,
                      self._const(k, 0.0))
        else:
            refuse("function '%s'" % self.fn, "a pointer used as a truth "
                   "value (there are no null pointers on the GPU)")
        u32 = m.t_int(32, False)
        return b.op("OpSelect", u32, nz, self.c1, self.c0)

    def ld(self, v):
        """(value id, kind) of an IL value held in its own kind."""
        if v.literal is not None:
            k = self.kind(v.ctype, "function '%s'" % self.fn)
            return self._lit_value(v, k), k
        k = self.vkind[v]
        return self.b.load(self.tid(k), self.var[v]), k

    def ld_as(self, v, dst):
        if v.literal is not None:
            return self._lit_value(v, dst)
        src = self.vkind[v]
        val = self.b.load(self.tid(src), self.var[v])
        return self._conv(val, src, dst)

    def st_(self, v, val):
        self.b.store(self.var[v], val)

    def _glsl(self):
        if self.glsl is None:
            self.glsl = self.m.ext_inst_import("GLSL.std.450")
        return self.glsl

    def _ext(self, name, rtype, *args):
        return self.b.op("OpExtInst", rtype, self._glsl(),
                         sp.GLSL_STD_450[name], *args)

    def _conv(self, val, src, dst):
        """Convert an id of kind `src` to kind `dst`, C conversion rules."""
        b = self.b
        m = self.m
        if src == dst:
            return val
        if src[0] == "p" or dst[0] == "p":
            if src[0] == "p" and dst[0] == "p":
                return val
            refuse("function '%s'" % self.fn,
                   "converting between a pointer and a number")
        if src[0] == "i" and dst[0] == "i":
            if src[1] == dst[1]:
                return val              # signless: same bits
            if src[1] == 32 and dst[1] == 64:
                op = "OpSConvert" if src[2] else "OpUConvert"
                return b.op(op, self.tid(dst), val)
            return b.op("OpUConvert", self.tid(dst), val)
        if src[0] == "i" and dst[0] == "f":
            op = "OpConvertSToF" if src[2] else "OpConvertUToF"
            return b.op(op, self.tid(dst), val)
        if src[0] == "f" and dst[0] == "i":
            if dst[1] != 32:
                refuse("function '%s'" % self.fn,
                       "float to 64-bit integer conversion")
            return self._f2i(val, dst[2])
        return val

    def _f2i(self, x, signed):
        """float -> 32-bit integer, saturating. C leaves an out-of-range
        conversion undefined and SPIR-V's OpConvertFToS is undefined there
        too; a GPU must not be handed an undefined result, so this clamps,
        sends NaN to 0 and pins the top end exactly -- the same choice the
        wasm back end makes (WASM.md, "Float-to-int uses the saturating
        conversion")."""
        m = self.m
        b = self.b
        ft = m.t_float(32)
        it = m.t_int(32, False)
        bt = m.t_bool()
        fc = m.const_float
        nan = b.op("OpFUnordNotEqual", bt, x, x)
        x0 = b.op("OpSelect", ft, nan, fc(ft, 0.0), x)
        if signed:
            c = self._ext("FClamp", ft, x0, fc(ft, -2147483648.0),
                          fc(ft, 2147483520.0))
            r = b.op("OpConvertFToS", it, c)
            big = b.op("OpFOrdGreaterThanEqual", bt, x0,
                       fc(ft, 2147483648.0))
            return b.op("OpSelect", it, big,
                        m.const_int(it, 0x7FFFFFFF), r)
        c = self._ext("FClamp", ft, x0, fc(ft, 0.0), fc(ft, 4294967040.0))
        r = b.op("OpConvertFToU", it, c)
        big = b.op("OpFOrdGreaterThanEqual", bt, x0, fc(ft, 4294967296.0))
        return b.op("OpSelect", it, big, m.const_int(it, 0xFFFFFFFF), r)

    # ------------------------------------------------------ command lowering

    def _emit_block(self, cmds, idx, total, label_block, single):
        for c in cmds:
            self._emit(c, idx, total, label_block, single)

    def _emit(self, c, idx, total, label_block, single):
        m = self.m
        b = self.b
        where = "function '%s'" % self.fn
        u32 = m.t_int(32, False)

        if isinstance(c, value_cmds.AddrOf):
            self.b.store(self.var[c.output], self.c0)
        elif isinstance(c, value_cmds.AddrRel):
            self._addr_rel(c)
        elif isinstance(c, value_cmds.ReadRel):
            self._rel_read(c)
        elif isinstance(c, value_cmds.SetRel):
            self._rel_set(c)
        elif isinstance(c, value_cmds.LoadArg):
            self._load_arg(c)
        elif isinstance(c, value_cmds.Set):
            if c.output.ctype.is_pointer():
                self.b.store(self.var[c.output], self.ld(c.arg)[0])
            elif c.output.ctype.is_bool() and not c.arg.ctype.is_bool():
                v, k = self.ld(c.arg)
                self.st_(c.output, self._truth(v, k))
            else:
                self.st_(c.output, self.ld_as(c.arg, self.vkind[c.output]))
        elif isinstance(c, value_cmds.ReadAt):
            self._read_at(c)
        elif isinstance(c, value_cmds.SetAt):
            self._set_at(c)
        elif isinstance(c, math_cmds.Add) or isinstance(c, math_cmds.Subtr):
            if c.output.ctype.is_pointer():
                self._ptr_arith(c)
            else:
                self._arith(c)
        elif isinstance(c, math_cmds._AddMult) or \
                isinstance(c, math_cmds._DivMod):
            self._arith(c)
        elif isinstance(c, math_cmds._BitShiftCmd):
            self._shift(c)
        elif isinstance(c, math_cmds._NegNot):
            self._negnot(c)
        elif isinstance(c, compare_cmds._GeneralCmp):
            self._compare(c)
        elif isinstance(c, control.Return):
            self._return(c)
        elif isinstance(c, control.Jump):
            self._goto(label_block[c.label])
        elif isinstance(c, control.JumpZero) or \
                isinstance(c, control.JumpNotZero):
            self._cond_jump(c, idx, total, label_block)
        elif isinstance(c, control.Call):
            self._call(c)
        else:
            refuse(where, "the IL command %s is not lowered yet"
                   % type(c).__name__)

    def _load_arg(self, c):
        m = self.m
        b = self.b
        if not self.is_kernel:
            if c.base is not None:
                refuse("function '%s'" % self.fn, "variadic arguments")
            self.st_(c.output, self.f.params[c.arg_num])
            return
        k = c.arg_num
        if c.output.ctype.is_pointer():
            if k not in self.bufs:
                refuse("kernel '%s'" % self.fn,
                       "parameter %d has no buffer" % k)
            b.store(self.var[c.output], self.c0)
            return
        i = self.push["index"][k]
        sk = self.push["scalars"][i][1]
        u32 = m.t_int(32, False)
        p = b.op("OpAccessChain", m.t_pointer("PushConstant", self.tid(sk)),
                 self.push["var"], m.const_int(u32, i))
        val = b.load(self.tid(sk), p)
        self.st_(c.output, self._conv(val, sk, self.vkind[c.output]))

    def _elem_access(self, addr, out_kind, where):
        """Pointer to the buffer element `addr` names, plus the element kind
        after checking the access has the buffer's type."""
        m = self.m
        b = self.b
        buf = self.bufs[self.base_of[addr]]
        ek = buf["kind"]
        if ek[0] != out_kind[0] or ek[1] != out_kind[1]:
            refuse(where, "accessing buffer '%s' as a different type "
                          "(reinterpreting a float buffer as int or the "
                          "reverse); the element type is the pointer's"
                   % buf["name"])
        u32 = m.t_int(32, False)
        off = b.load(u32, self.var[addr])
        idx = b.op("OpShiftRightLogical", u32, off, m.const_int(u32, 2))
        if buf.get("storage") == "Workgroup":
            if buf["count"] is None:
                return buf["var"], ek       # a shared scalar: the variable
            p = b.op("OpAccessChain",
                     m.t_pointer("Workgroup", self.tid(ek)), buf["var"], idx)
            return p, ek
        p = b.op("OpAccessChain", m.t_pointer("StorageBuffer", self.tid(ek)),
                 buf["var"], self.c0, idx)
        return p, ek

    def _read_at(self, c):
        where = "function '%s'" % self.fn
        ok = self.vkind[c.output]
        p, ek = self._elem_access(c.addr, ok, where)
        self.st_(c.output, self.b.load(self.tid(ek), p))

    def _set_at(self, c):
        where = "function '%s'" % self.fn
        vk = self.kind(c.val.ctype, where)
        p, ek = self._elem_access(c.addr, vk, where)
        self.b.store(p, self.ld_as(c.val, ek))

    def _ptr_arith(self, c):
        m = self.m
        b = self.b
        u32 = m.t_int(32, False)
        pa = c.arg1.ctype.is_pointer()
        p = c.arg1 if pa else c.arg2
        n = c.arg2 if pa else c.arg1
        off = b.load(u32, self.var[p])
        nv = self.ld_as(n, U32)
        op = "OpIAdd" if isinstance(c, math_cmds.Add) else "OpISub"
        self.st_(c.output, b.op(op, u32, off, nv))

    def _arith(self, c):
        b = self.b
        where = "function '%s'" % self.fn
        ok = self.vkind[c.output]
        t = self.tid(ok)
        x = self.ld_as(c.arg1, ok)
        y = self.ld_as(c.arg2, ok)
        n = type(c).__name__
        if ok[0] == "f":
            ops = {"Add": "OpFAdd", "Subtr": "OpFSub", "Mult": "OpFMul",
                   "Div": "OpFDiv"}
            if n not in ops:
                refuse(where, "%s on floating-point values" % n)
            self.st_(c.output, b.op(ops[n], t, x, y))
            return
        signed = ok[2]
        ops = {"Add": "OpIAdd", "Subtr": "OpISub", "Mult": "OpIMul",
               "BitAnd": "OpBitwiseAnd", "BitOr": "OpBitwiseOr",
               "BitXor": "OpBitwiseXor"}
        if n in ops:
            self.st_(c.output, b.op(ops[n], t, x, y))
        elif n == "Div":
            self.st_(c.output, b.op("OpSDiv" if signed else "OpUDiv",
                                    t, x, y))
        elif n == "Mod":
            self.st_(c.output, b.op("OpSRem" if signed else "OpUMod",
                                    t, x, y))
        else:
            refuse(where, "the operation %s" % n)

    def _shift(self, c):
        """C leaves a shift count at or past the width undefined, and so does
        SPIR-V; the count is masked to the width (what C# defines, and what
        every CPU does), so no count can reach the undefined case."""
        b = self.b
        m = self.m
        ok = self.vkind[c.output]
        t = self.tid(ok)
        x = self.ld_as(c.arg1, ok)
        cnt = self.ld_as(c.arg2, ok)
        cnt = b.op("OpBitwiseAnd", t, cnt, self._const(ok, ok[1] - 1))
        if isinstance(c, math_cmds.LBitShift):
            op = "OpShiftLeftLogical"
        else:
            op = "OpShiftRightArithmetic" if ok[2] else "OpShiftRightLogical"
        self.st_(c.output, b.op(op, t, x, cnt))

    def _negnot(self, c):
        b = self.b
        ok = self.vkind[c.output]
        t = self.tid(ok)
        x = self.ld_as(c.arg, ok)
        if isinstance(c, math_cmds.Neg):
            op = "OpFNegate" if ok[0] == "f" else "OpSNegate"
        else:
            if ok[0] == "f":
                refuse("function '%s'" % self.fn, "bitwise not of a float")
            op = "OpNot"
        self.st_(c.output, b.op(op, t, x))

    def _compare(self, c):
        m = self.m
        b = self.b
        where = "function '%s'" % self.fn
        k1 = self.kind(c.arg1.ctype, where)
        k2 = self.kind(c.arg2.ctype, where)
        if k1[0] == "p" or k2[0] == "p":
            refuse(where, "comparing pointers")
        # The usual arithmetic conversions have already been applied by the
        # front end; convert the second operand to the first's kind so a
        # leftover mismatch cannot reach OpIEqual with two different types.
        x = self.ld_as(c.arg1, k1)
        y = self.ld_as(c.arg2, k1)
        n = type(c).__name__
        bt = m.t_bool()
        if k1[0] == "f":
            ops = {"EqualCmp": "OpFOrdEqual", "NotEqualCmp": "OpFUnordNotEqual",
                   "LessCmp": "OpFOrdLessThan",
                   "GreaterCmp": "OpFOrdGreaterThan",
                   "LessOrEqCmp": "OpFOrdLessThanEqual",
                   "GreaterOrEqCmp": "OpFOrdGreaterThanEqual"}
            r = b.op(ops[n], bt, x, y)
        else:
            if n == "EqualCmp":
                r = b.op("OpIEqual", bt, x, y)
            elif n == "NotEqualCmp":
                r = b.op("OpINotEqual", bt, x, y)
            else:
                pair = {"LessCmp": ("OpSLessThan", "OpULessThan"),
                        "GreaterCmp": ("OpSGreaterThan", "OpUGreaterThan"),
                        "LessOrEqCmp": ("OpSLessThanEqual",
                                        "OpULessThanEqual"),
                        "GreaterOrEqCmp": ("OpSGreaterThanEqual",
                                           "OpUGreaterThanEqual")}[n]
                r = b.op(pair[0] if k1[2] else pair[1], bt, x, y)
        ok = self.vkind[c.output]
        out = b.op("OpSelect", self.tid(ok), r, self._const(ok, 1),
                   self._const(ok, 0))
        self.st_(c.output, out)

    def _cond_jump(self, c, idx, total, label_block):
        m = self.m
        b = self.b
        u32 = m.t_int(32, False)
        k = self.kind(c.cond.ctype, "function '%s'" % self.fn)
        if k[0] != "i":
            refuse("function '%s'" % self.fn,
                   "a branch on a non-integer condition")
        v = self.ld_as(c.cond, k)
        is_zero = b.op("OpIEqual", m.t_bool(), v, self._const(k, 0))
        target = label_block[c.label]
        fall = idx + 1
        if fall >= total:
            # The fall-through is the end of the function: give it a block
            # to land in so the select has a state to pick.
            refuse("function '%s'" % self.fn,
                   "a conditional jump as the last command")
        t = m.const_int(u32, target)
        f = m.const_int(u32, fall)
        if isinstance(c, control.JumpZero):
            nxt = b.op("OpSelect", u32, is_zero, t, f)
        else:
            nxt = b.op("OpSelect", u32, is_zero, f, t)
        b.store(self.state, nxt)
        b.branch(self.sw_merge.label)

    def _return(self, c):
        where = "function '%s'" % self.fn
        if self.is_kernel:
            if c.arg is not None:
                refuse(where, "a kernel returns nothing")
            self.b.ret()
            return
        ct = self._sig(self.fn)
        if ct.ret.is_void():
            self.b.ret()
            return
        rk = self.kind(ct.ret, where)
        if c.arg is None:
            self.b.op("OpReturnValue", 0, self._const(rk, 0))
        elif ct.ret.is_bool() and not c.arg.ctype.is_bool():
            v, k = self.ld(c.arg)
            self.b.op("OpReturnValue", 0, self._truth(v, k))
        else:
            self.b.op("OpReturnValue", 0, self.ld_as(c.arg, rk))

    def _builtin_var(self, name):
        if name not in self.builtin_vars:
            m = self.m
            u32 = m.t_int(32, False)
            v3 = m.t_vector(u32, 3)
            var = m.global_var(m.t_pointer("Input", v3), "Input")
            m.decorate(var, "BuiltIn", sp.BUILTIN[name])
            m.name(var, "gl_" + name)
            self.builtin_vars[name] = var
        return self.builtin_vars[name]

    def _call(self, c):
        m = self.m
        b = self.b
        where = "function '%s'" % self.fn
        nm = c.direct_name
        u32 = m.t_int(32, False)
        if nm == "gpu_barrier":
            self._barrier()
            return
        if nm == "gpu_popcount":
            if len(c.args) != 1:
                refuse(where, "gpu_popcount takes one argument")
            v = self.ld_as(c.args[0], U32)
            r = b.op("OpBitCount", u32, v)
            if c.ret is not None:
                self.st_(c.ret, self._conv(r, U32, self.vkind[c.ret]))
            return
        if nm in GPU_ATOMICS:
            self._atomic(c, nm)
            return
        if nm in GPU_BUILTINS:
            if len(c.args) != 1 or c.args[0].literal is None:
                refuse(where, "%s() takes a constant dimension 0, 1 or 2"
                       % nm)
            dim = int(c.args[0].literal.val)
            if dim < 0 or dim > 2:
                refuse(where, "%s(%d): the dimension must be 0, 1 or 2"
                       % (nm, dim))
            bname = GPU_BUILTINS[nm]
            var = self._builtin_var(bname)
            self.fn_builtins[self.fn][bname] = True
            p = b.op("OpAccessChain", m.t_pointer("Input", u32), var,
                     m.const_int(u32, dim))
            val = b.load(u32, p)
            if c.ret is not None:
                self.st_(c.ret, self._conv(val, U32, self.vkind[c.ret]))
            return
        if nm in GPU_MATH:
            code, arity = GPU_MATH[nm]
            if len(c.args) != arity:
                refuse(where, "%s takes %d argument(s)" % (nm, arity))
            args = []
            for a in c.args:
                args.append(self.ld_as(a, F32))
            ft = m.t_float(32)
            r = self._ext(code, ft, *args)
            if c.ret is not None:
                self.st_(c.ret, self._conv(r, F32, self.vkind[c.ret]))
            return
        callee = self._sig(nm)
        args = []
        for i in range(len(c.args)):
            pk = self.kind(callee.args[i], where)
            args.append(self.ld_as(c.args[i], pk))
        if callee.ret.is_void():
            b.op("OpFunctionCall", m.t_void(), self.fobj[nm].id, *args)
            return
        rk = self.kind(callee.ret, where)
        r = b.op("OpFunctionCall", self.tid(rk), self.fobj[nm].id, *args)
        if c.ret is not None:
            self.st_(c.ret, self._conv(r, rk, self.vkind[c.ret]))

    # -------------------------------------------------------- entry points

    def _closure_builtins(self, fn):
        seen = {}
        out = {}
        stack = [fn]
        while stack:
            f = stack.pop()
            if f in seen:
                continue
            seen[f] = True
            for b in self.fn_builtins.get(f, {}):
                out[b] = True
            for callee in self.calls.get(f, []):
                stack.append(callee)
        return out

    def _entry_point(self, fn):
        m = self.m
        iface = []
        for b in sorted(self._closure_builtins(fn)):
            iface.append(self.builtin_vars[b])
        m.entry_point(self.fobj[fn], fn, "GLCompute", iface)
        ls = self.info.gpu_compute[fn]["local_size"]
        m.local_size(self.fobj[fn], ls[0], ls[1], ls[2])
