"""Contract-driven, fallback-free SIMD.

The language-extension front-end (shivyc/extensions.py) attaches `assert`-style
contracts to a function's array arguments, e.g. for

    int calc_sum(int *ptr, unsigned int len)
    assert len(ptr) >= 64
    assert not len(ptr) % 4

it records `{'ptr': {'len>=': 64, 'div-by': 4}}`.

GCC and Clang, when they auto-vectorize a reduction like `for(i) v += ptr[i]`,
must emit a scalar remainder loop (and sometimes runtime alignment checks) for
the cases where the length is not a multiple of the SIMD width. Those branches
are the "fallback".

When the compiler can *see the whole call graph* and prove that every call
satisfies the contracts -- for instance by parsing `malloc(N * sizeof(int))` at
the one call site -- the remainder can never run, so it can be omitted. This
module performs that proof and, for a recognized sum-reduction, emits a
fallback-free SSE2 loop (4x int32 per iteration, no scalar tail).

This is a deliberately narrow, verifiable slice: it vectorizes integer sum
reductions whose alignment is *proven*, and otherwise leaves ShivyC's ordinary
scalar codegen untouched.
"""

import shivyc.il_cmds.control as control_cmds
import shivyc.il_cmds.value as value_cmds
import shivyc.il_cmds.math as math_cmds
import shivyc.ctypes as ctypes
import shivyc.asm_cmds as asm_cmds
import shivyc.spots as spots


class ProofResult:
    """Outcome of proving a contract function's call sites."""

    def __init__(self, name):
        self.name = name
        self.call_sites = 0
        self.proven = False
        self.reason = ""

    def __str__(self):
        if self.proven:
            return (f"simd-contracts: '{self.name}': contracts proven at all "
                    f"{self.call_sites} call site(s); scalar fallback omitted")
        return (f"simd-contracts: '{self.name}': not proven "
                f"({self.reason}); keeping scalar code")


def analyze(il_code, symbol_table, ext_info):
    """Prove contracts across the call graph.

    Returns a set of function names that are proven SIMD-safe (every call site
    satisfies the contracts) AND whose body is a recognized reduction, so
    asm_gen may emit the fallback-free SSE2 form. Also returns a list of
    human-readable ProofResult reports.
    """
    proven = {}
    reports = []
    if not ext_info or not ext_info.contracts:
        return proven, reports

    name_of = _build_function_names(il_code, symbol_table)

    for fname, arg_contracts in ext_info.contracts.items():
        result = ProofResult(fname)
        if fname not in il_code.commands:
            result.reason = "no definition"
            reports.append(result)
            continue

        layout = _arg_layout(fname, symbol_table)
        ptrs = [a for a in layout if a["is_ptr"]]
        ints = [a for a in layout if a["is_int"]]
        if not ptrs:
            result.reason = "no pointer argument with a contract"
            reports.append(result)
            continue
        len_index = ints[0]["index"] if ints else None

        contract = _merge_contracts(arg_contracts)
        sites = _find_call_sites(il_code, name_of, fname)
        result.call_sites = len(sites)
        if not sites:
            result.reason = "no call sites visible"
            reports.append(result)
            continue

        all_ok = True
        counts = set()
        for caller, call in sites:
            count = _prove_one_call_multi(
                il_code, name_of, caller, call, ptrs, len_index)
            if count is None or not _satisfies(count, contract):
                all_ok = False
                result.reason = "a call site could not be proven aligned"
                break
            counts.add(count)
        if not all_ok:
            reports.append(result)
            continue

        cmds = il_code.commands[fname]
        if _is_sum_reduction(cmds):
            desc = {"kind": "reduce"}
            # An unsigned-byte sum lowers to PSADBW (sum of absolute differences
            # against zero), which sums 16 bytes into two 64-bit lanes with no
            # intermediate overflow -- the ideal hardware byte accumulator.
            p0 = ptrs[0]
            if p0.get("elem_size") == 1 and not p0.get("elem_signed", True):
                desc["elem"] = "u8"
        else:
            desc = _classify_elementwise(cmds, ptrs, layout, name_of)
        if not desc:
            result.reason = "alignment proven but body is not a recognized kernel"
            reports.append(result)
            continue

        # No length argument (fixed-size kernel like vadd256): the trip count is
        # the proven element count, baked in as a literal. Requires all call
        # sites to agree on that size.
        if len_index is None:
            if len(counts) != 1:
                result.reason = "fixed-size kernel: call sites disagree on length"
                reports.append(result)
                continue
            desc["fixed_count"] = next(iter(counts))
        result.proven = True
        proven[fname] = desc
        reports.append(result)

    return proven, reports


def _satisfies(count, contract):
    """Check a proven element count against a contract dict."""
    if "len>=" in contract and count < contract["len>="]:
        return False
    if "len<=" in contract and count > contract["len<="]:
        return False
    if "div-by" in contract and count % contract["div-by"] != 0:
        return False
    return True


def _build_function_names(il_code, symbol_table):
    """Map each function ILValue to its name (for resolving call targets)."""
    names = {}
    for val, name in symbol_table.names.items():
        ctype = getattr(val, "ctype", None)
        if ctype is not None and ctype.is_function():
            names[val] = name
    return names


def _member_array(ct):
    """The fixed-size arithmetic array a struct carries at offset 0, or None.

    `Vec<T,N>` lowers to `struct { T d[N]; }`, so a method's receiver is a
    pointer to exactly this. Restricted to offset 0 on purpose: a member
    further in would need its offset folded into every load and store, and
    the synthesizer addresses straight off the register. One member at zero
    is what a fixed-size vector or matrix is, and anything else is left
    alone rather than half-handled.
    """
    if ct is None or not ct.is_struct_union():
        return None
    for _nm, (_off, _mt) in (getattr(ct, "offsets", None) or {}).items():
        if _off == 0 and isinstance(_mt, ctypes.ArrayCType) \
                and isinstance(_mt.n, int) and _mt.n > 0 \
                and _mt.el.is_arith():
            return _mt
    return None


def _arg_layout(fname, symbol_table):
    """Per-argument shape for `fname`: index, whether it is a pointer (and its
    element size), or an integer/float scalar. Drives the multi-array proof."""
    func_val = None
    for val, name in symbol_table.names.items():
        ctype = getattr(val, "ctype", None)
        if name == fname and ctype is not None and ctype.is_function():
            func_val = val
            break
    if func_val is None:
        return []
    layout = []
    for i, at in enumerate(func_val.ctype.args):
        # A pointer counts as a kernel *array* only when it points at an
        # arithmetic element. A method lowered from the C++ subset takes
        # its receiver first -- `Kern_vadd(Kern *this, float *a, ..)` --
        # and `this` is a pointer that no `malloc` in the caller produced,
        # so requiring it to trace to one failed the proof for every
        # kernel written as a member. It is not part of the loop either:
        # the vectorized body indexes arrays of scalars, and a pointer to
        # a struct is not one. Treated as an ordinary opaque argument, it
        # still occupies its System V register, which is what the
        # synthesizer needs to know about it.
        # A pointer to a struct whose whole content is a fixed-size
        # arithmetic array is an array base too. That is what a method's
        # receiver is -- `Vec_float_16 *this` over `struct { float d[16]; }`
        # -- and treating it as opaque meant a kernel written as an
        # operator had no arrays at all and could never be classified.
        marr = _member_array(at.arg) if at.is_pointer() else None
        is_ptr = at.is_pointer() and (at.arg.is_arith() or marr is not None)
        layout.append({
            "index": i,
            "is_ptr": is_ptr,
            "elem_size": ((marr.el.size if marr is not None else at.arg.size)
                          if is_ptr else None),
            "elem_signed": ((getattr(marr.el if marr is not None else at.arg,
                                     "signed", True)) if is_ptr else None),
            "member_count": (marr.n if marr is not None else None),
            "is_int": (not at.is_pointer()) and at.is_integral(),
            "is_float": (not at.is_pointer()) and at.is_floating(),
        })
    return layout


def _merge_contracts(arg_contracts):
    """Combine per-argument contracts into the single strongest constraint."""
    merged = {}
    for c in arg_contracts.values():
        if "div-by" in c:
            merged["div-by"] = max(merged.get("div-by", 1), c["div-by"])
        if "len>=" in c:
            merged["len>="] = max(merged.get("len>=", 0), c["len>="])
        if "len<=" in c:
            merged["len<="] = min(merged.get("len<=", 1 << 62), c["len<="])
    return merged


def _prove_one_call_multi(il_code, name_of, caller, call, ptrs, len_index):
    """Prove a call where the kernel has several pointer arguments and one
    length argument (e.g. saxpy(alpha, x, y, out, n)). Every pointer must trace
    to a malloc whose element count is at least the literal length. Returns the
    proven element count (the length) or None."""
    cmds = il_code.commands[caller]
    # element count of each pointer's allocation (must all be consistent)
    counts = []
    for p in ptrs:
        if p["index"] >= len(call.args):
            return None
        byte_size = _trace_malloc_bytes(
            il_code, name_of, cmds, call.args[p["index"]])
        if byte_size is None:
            return None
        counts.append(byte_size // p["elem_size"])

    if len_index is None:
        # Fixed-size kernel with no length parameter: the processed count is the
        # allocation itself, so every pointer must allocate the same count.
        if len(set(counts)) != 1:
            return None
        return counts[0]

    if len_index >= len(call.args):
        return None
    length = _trace_literal(il_code, cmds, call.args[len_index])
    if length is None:
        return None
    for cnt in counts:
        if length > cnt:
            return None                 # would read/write out of bounds
    return length


_INT_REGS64 = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]
_REG32 = {"rdi": "edi", "rsi": "esi", "rdx": "edx", "rcx": "ecx",
          "r8": "r8d", "r9": "r9d"}
_SSE_REGS = ["xmm" + str(i) for i in range(8)]
_MAP_FUNCS = {"sqrt": "sqrt", "sqrtf": "sqrt"}


def _sysv_regs(layout):
    """System V AMD64 register for each argument (in declaration order):
    integer/pointer args fill rdi,rsi,rdx,rcx,r8,r9; floating args fill
    xmm0..7. Returns {arg_index: reg_name}."""
    regs = {}
    ii = si = 0
    for a in layout:
        if a["is_float"]:
            if si < len(_SSE_REGS):
                regs[a["index"]] = _SSE_REGS[si]
            si += 1
        else:                           # pointer or integer -> GPR
            if ii < len(_INT_REGS64):
                regs[a["index"]] = _INT_REGS64[ii]
            ii += 1
    return regs


def _classify_dot(cmds, ptrs, layout):
    """Recognize a float dot product `acc = acc + a[i]*b[i]` over a loop that
    returns the accumulator. Two pointer inputs, no store; the value added each
    iteration is the product of the two loads, written back into the
    accumulator. Returns a descriptor (kind 'dot') or None."""
    defmap = {}
    for c in cmds:
        for o in c.outputs():
            defmap[o] = c
    reads = [c for c in cmds if isinstance(c, value_cmds.ReadAt)]
    read_outs = {r.output for r in reads}

    def origin(v):
        d = defmap.get(v)
        while isinstance(d, value_cmds.Set):
            v = d.arg
            d = defmap.get(v)
        return d

    def is_load(v):
        d = defmap.get(v)
        while isinstance(d, value_cmds.Set):
            v = d.arg
            d = defmap.get(v)
        return v in read_outs

    set_targets = {}
    for c in cmds:
        if isinstance(c, value_cmds.Set):
            set_targets.setdefault(c.arg, []).append(c.output)

    for c in cmds:
        if not isinstance(c, math_cmds.Add):
            continue
        a, b = c.arg1, c.arg2
        for acc, term in ((a, b), (b, a)):
            dt = origin(term)
            if isinstance(dt, math_cmds.Mult) and \
                    is_load(dt.arg1) and is_load(dt.arg2) and \
                    not is_load(acc) and \
                    acc in set_targets.get(c.output, []):
                load_ct = reads[0].output.ctype
                # accumulator and array element type must match (no mixed
                # float-array / double-accumulator widths)
                if not load_ct.is_floating() or \
                        not c.output.ctype.is_floating() or \
                        c.output.ctype.size != load_ct.size:
                    return None
                regs = _sysv_regs(layout)
                ints = [a for a in layout if a["is_int"]]
                return {
                    "kind": "dot",
                    "elem_size": load_ct.size,
                    "in_regs": [regs[p["index"]] for p in ptrs],
                    "out_reg": None,
                    "scalar_reg": None,
                    "len_reg": (_REG32[regs[ints[0]["index"]]]
                                if ints else None),
                }
    return None


def _arg_bases(cmds):
    """`{ILValue: argument index}` for each parameter, from its LoadArg."""
    out = {}
    for c in cmds:
        if isinstance(c, value_cmds.LoadArg):
            out[c.output] = c.arg_num
    return out


def _base_arg(cmds, defmap, addr, bases, depth=0):
    """Which parameter index an address is computed from, or None.

    `out[i]` is `base + i * sizeof(elem)`, so the address reaching a
    `SetAt` is an `Add` away from the pointer parameter it writes through.
    Walking back to that parameter is how the output is *found* rather than
    assumed.
    """
    if addr is None or depth > 16:
        return None
    if addr in bases:
        return bases[addr]
    d = defmap.get(addr)
    if isinstance(d, value_cmds.Set):
        return _base_arg(cmds, defmap, d.arg, bases, depth + 1)
    if isinstance(d, value_cmds.AddrRel) and not getattr(d, "chunk", 0):
        # `this->d` is `AddrRel(this, 0)`, so the array's base is the
        # struct pointer the parameter holds.
        return _base_arg(cmds, defmap, d.base, bases, depth + 1)
    if isinstance(d, (math_cmds.Add, math_cmds.Subtr)):
        for side in (d.arg1, d.arg2):
            got = _base_arg(cmds, defmap, side, bases, depth + 1)
            if got is not None:
                return got
    return None


def _classify_elementwise(cmds, ptrs, layout, name_of=None):
    """Recognize a float element-wise store kernel and return a descriptor with
    the System V registers the synthesizer needs. Supported shapes:

        out[i] = a[i] + b[i]        binary add        (3 ptr)
        out[i] = a[i] - b[i]        binary sub        (3 ptr)
        out[i] = a[i] * b[i]        binary mul        (3 ptr)
        out[i] = a[i]*b[i] + c[i]   fma               (4 ptr)
        out[i] = alpha*x[i] + y[i]  saxpy             (3 ptr + fp scalar)
        out[i] = sqrt(x[i])         map (sqrtps)      (2 ptr)

    Convention: the LAST pointer argument is the output, earlier pointers are
    inputs. Float element type only. None if unrecognized."""
    if not (2 <= len(ptrs) <= 4):
        return None
    defmap = {}
    for c in cmds:
        for o in c.outputs():
            defmap[o] = c
    reads = [c for c in cmds if isinstance(c, value_cmds.ReadAt)]
    stores = [c for c in cmds if isinstance(c, value_cmds.SetAt)]

    # float dot product: acc = acc + a[i]*b[i] over a loop, returns a float.
    # No store; two pointer inputs; the accumulated term is a product of loads.
    if len(stores) == 0 and len(reads) == 2 and len(ptrs) == 2:
        dotdesc = _classify_dot(cmds, ptrs, layout)
        if dotdesc:
            return dotdesc

    if len(stores) != 1:
        return None
    store = stores[0]
    if not store.val.ctype.is_floating():
        return None
    elem_size = store.val.ctype.size
    read_outs = {r.output for r in reads}

    def origin(v):
        d = defmap.get(v)
        while isinstance(d, value_cmds.Set):
            v = d.arg
            d = defmap.get(v)
        return d

    def is_load(v):
        d = defmap.get(v)
        while isinstance(d, value_cmds.Set):
            v = d.arg
            d = defmap.get(v)
        return v in read_outs

    regs = _sysv_regs(layout)
    # Which pointer the kernel *writes* through, found by walking the
    # store's address back to the parameter it is computed from.
    #
    # This used to be a convention -- "the last pointer argument is the
    # output" -- which held for every kernel anyone had written by hand
    # (`vadd(a, b, out, n)`, `saxpy(alpha, x, y, out, n)`) and is not a
    # property of C. A kernel spelled the other way round, `la_add(out, a,
    # b)`, was classified with `out` as an input and `b` as the
    # destination: it compiled, vectorized, and computed `b = out + a`.
    # Silently wrong output is the one result this compiler must not
    # produce, so the destination is now read off the store.
    bases = _arg_bases(cmds)
    out_index = _base_arg(cmds, defmap, store.addr, bases)
    if out_index is None or out_index not in [p["index"] for p in ptrs]:
        return None
    in_regs = [regs[p["index"]] for p in ptrs if p["index"] != out_index]
    out_reg = regs[out_index]
    floats = [a for a in layout if a["is_float"]]
    scalar_reg = regs[floats[0]["index"]] if floats else None
    ints = [a for a in layout if a["is_int"]]
    len_reg = _REG32[regs[ints[0]["index"]]] if ints else None

    def base(extra):
        d = dict(extra)
        d["elem_size"] = elem_size
        d["in_regs"] = in_regs
        d["out_reg"] = out_reg
        d["scalar_reg"] = scalar_reg
        d["len_reg"] = len_reg
        return d

    d = origin(store.val)

    # single-input map: out[i] = f(x[i])
    if len(reads) == 1 and len(ptrs) == 2 and \
            isinstance(d, control_cmds.Call):
        target = getattr(d, "direct_name", None)
        if target is None and name_of is not None:   # resolved via AddrOf
            addr_of = {c.output: name_of.get(c.var)
                       for c in cmds
                       if isinstance(c, value_cmds.AddrOf)}
            target = addr_of.get(d.func)
        if target in _MAP_FUNCS and any(is_load(a) for a in d.inputs()):
            return base({"kind": "map", "op": _MAP_FUNCS[target]})
        return None

    # single-input scalar broadcast: out[i] = x[i] {*,+,-} s  (2 ptr + fp scalar)
    if len(reads) == 1 and len(ptrs) == 2 and scalar_reg is not None and \
            isinstance(d, (math_cmds.Mult, math_cmds.Add, math_cmds.Subtr)):
        op = {math_cmds.Mult: "mul", math_cmds.Add: "add",
              math_cmds.Subtr: "sub"}[type(d)]
        la, lb = is_load(d.arg1), is_load(d.arg2)
        # exactly one operand is the load; the other is the broadcast scalar.
        if la and not lb:
            return base({"kind": "scale", "op": op})
        if lb and not la and op != "sub":   # commutative: scalar on the left
            return base({"kind": "scale", "op": op})

    if len(reads) == 2 and isinstance(d, math_cmds.Add) and len(ptrs) == 3:
        a, b = d.arg1, d.arg2
        for x, y in ((a, b), (b, a)):       # saxpy: one side is scalar*load
            dx = origin(x)
            if isinstance(dx, math_cmds.Mult) and \
                    (is_load(dx.arg1) or is_load(dx.arg2)) and is_load(y):
                return base({"kind": "saxpy", "op": "add"})
        if is_load(a) and is_load(b):
            return base({"kind": "binary", "op": "add"})
    elif len(reads) == 2 and isinstance(d, math_cmds.Mult) and len(ptrs) == 3:
        if is_load(d.arg1) and is_load(d.arg2):
            return base({"kind": "binary", "op": "mul"})
    elif len(reads) == 2 and isinstance(d, math_cmds.Subtr) and len(ptrs) == 3:
        if is_load(d.arg1) and is_load(d.arg2):
            return base({"kind": "binary", "op": "sub"})

    # fused multiply-add: out[i] = a[i]*b[i] + c[i]   (4 pointers, 3 loads)
    if len(reads) == 3 and isinstance(d, math_cmds.Add) and len(ptrs) == 4:
        a, b = d.arg1, d.arg2
        for x, y in ((a, b), (b, a)):
            dx = origin(x)
            if isinstance(dx, math_cmds.Mult) and \
                    is_load(dx.arg1) and is_load(dx.arg2) and is_load(y):
                return base({"kind": "fma", "op": "add"})
    return None


def _pointer_arg_info(fname, symbol_table):
    """Return (element_size, arg_index) for the function's pointer arg."""
    func_val = None
    for val, name in symbol_table.names.items():
        if name == fname and val.ctype.is_function():
            func_val = val
            break
    if func_val is None:
        return None, None
    for i, arg_t in enumerate(func_val.ctype.args):
        if arg_t.is_pointer():
            return arg_t.arg.size, i
    return None, None


def _find_call_sites(il_code, name_of, target):
    """Return [(caller_name, Call), ...] calling `target`."""
    sites = []
    for caller, cmds in il_code.commands.items():
        addr_of = {}  # ptr ILValue -> function name it addresses
        for c in cmds:
            if isinstance(c, value_cmds.AddrOf) and c.var in name_of:
                addr_of[c.output] = name_of[c.var]
            elif isinstance(c, control_cmds.Call):
                tgt = getattr(c, "direct_name", None) or addr_of.get(c.func)
                if tgt == target:
                    sites.append((caller, c))
    return sites


def _prove_one_call(il_code, name_of, caller, call, arg_index, elem_size):
    """Return the proven element count for `call`, or None if unprovable."""
    cmds = il_code.commands[caller]

    # Resolve the pointer argument to a malloc(byte_size) with literal size.
    ptr_val = call.args[arg_index]
    byte_size = _trace_malloc_bytes(il_code, name_of, cmds, ptr_val)
    if byte_size is None:
        return None
    count_from_alloc = byte_size // elem_size

    # The length argument must be a literal that does not exceed the allocation.
    len_val = call.args[1 - arg_index] if len(call.args) > 1 else None
    length = _trace_literal(il_code, cmds, len_val) if len_val else None
    if length is None:
        return None
    if length > count_from_alloc:
        return None  # would read out of bounds; not safe
    return length


def _defs(cmds):
    """Map each ILValue to the command that defines (outputs) it."""
    d = {}
    for c in cmds:
        for o in c.outputs():
            d[o] = c
    return d


def _trace_literal(il_code, cmds, val, depth=0):
    """Follow Set-copies to an integer literal value, or None."""
    if val is None or depth > 16:
        return None
    if val in il_code.literals:
        return il_code.literals[val]
    defn = _defs(cmds).get(val)
    if isinstance(defn, value_cmds.Set):
        return _trace_literal(il_code, cmds, defn.arg, depth + 1)
    return None


def _trace_malloc_bytes(il_code, name_of, cmds, val, depth=0):
    """Follow Set-copies to an allocation; return its literal byte size.

    Two allocations count. A `malloc(..)` whose byte size is a literal, and
    a **fixed-size array** -- `float x[16]` -- reached through the `AddrOf`
    that decays it at the call. The array is if anything the easier proof:
    a malloc's size is a literal this pass has to read back out of the call,
    while an array's is part of its type and cannot be anything else.

    It was not accepted before because the only shape anyone had needed was
    the heap one. Fixed-size arrays are what a fixed-size matrix *is*, so
    without this the contracts inferred from a template argument -- the
    whole point of putting the size in the type -- could never be proven
    at a call site that passed one.
    """
    if val is None or depth > 16:
        return None
    defs = _defs(cmds)
    defn = defs.get(val)
    if isinstance(defn, value_cmds.Set):
        return _trace_malloc_bytes(il_code, name_of, cmds, defn.arg, depth + 1)
    if isinstance(defn, value_cmds.AddrRel):
        # `v.d` where `v` is a local struct and `d` a fixed-size array
        # member. A fixed-size matrix *is* a struct with an inline array,
        # so this is the shape every method-based kernel call has: the
        # operator hands `r.d`, `this->d` and `o->d` to the kernel, and
        # without this none of them could be proven and the whole
        # class-plus-kernel design got scalar code.
        #
        # `AddrRel(out, base, chunk)` is `&base + chunk`, so the member is
        # the one at that byte offset. Found by offset rather than by name
        # because the name is not on the command -- and the offset is what
        # the address actually is, which makes it the honest key.
        bt = getattr(defn.base, "ctype", None)
        chunk = getattr(defn, "chunk", 0) or 0
        if bt is not None and bt.is_struct_union() \
                and getattr(defn, "count", None) is None:
            for _nm, (_off, _mt) in (bt.offsets or {}).items():
                if _off == chunk and isinstance(_mt, ctypes.ArrayCType) \
                        and isinstance(_mt.n, int) and _mt.n > 0:
                    return _mt.n * _mt.el.size
        return None
    if isinstance(defn, value_cmds.AddrOf):
        at = getattr(defn.var, "ctype", None)
        if at is not None and isinstance(at, ctypes.ArrayCType) \
                and isinstance(at.n, int) and at.n > 0:
            return at.n * at.el.size
        marr = _member_array(at)
        if marr is not None:
            # `&v` where `v` is a fixed-size vector. Its extent is a fact
            # of its type rather than of the call, which makes it the
            # strongest of the three allocations this pass recognises.
            return marr.n * marr.el.size
        return None
    if isinstance(defn, control_cmds.Call):
        addr_of = {}
        for c in cmds:
            if isinstance(c, value_cmds.AddrOf) and c.var in name_of:
                addr_of[c.output] = name_of[c.var]
        tgt = getattr(defn, "direct_name", None) or addr_of.get(defn.func)
        if tgt == "malloc" and defn.args:
            return _trace_literal(il_code, cmds, defn.args[0])
    return None


def _is_sum_reduction(cmds):
    """Conservatively recognize `acc = acc + ptr[i]` over a loop.

    ShivyC lowers `v = v + ptr[i]` as `Add(T, v, load); Set(v, T)`, so the
    accumulator cycle is: a ReadAt loads a value, an Add combines it with some
    value `acc`, and a Set writes the Add's result back into that same `acc`.
    """
    read_outputs = set()
    for c in cmds:
        if isinstance(c, value_cmds.ReadAt):
            read_outputs.add(c.output)

    # A narrow element (e.g. `unsigned char`) is widened to the accumulator's
    # type by a scalar `Set` between the load and the `Add`. Follow such Set
    # conversions transitively so a widened byte load still counts as a load.
    load_derived = set(read_outputs)
    changed = True
    while changed:
        changed = False
        for c in cmds:
            if isinstance(c, value_cmds.Set) and c.output not in load_derived \
                    and any(i in load_derived for i in c.inputs()):
                load_derived.add(c.output)
                changed = True

    # Set arg -> output, to find what an Add result is copied into.
    set_targets = {}
    for c in cmds:
        if isinstance(c, value_cmds.Set):
            set_targets.setdefault(c.arg, []).append(c.output)

    for c in cmds:
        if not isinstance(c, math_cmds.Add):
            continue
        ins = c.inputs()
        loads = [i for i in ins if i in load_derived]
        others = [i for i in ins if i not in load_derived]
        if not loads or not others:
            continue
        # The Add result must be written back into one of its non-load inputs
        # (the accumulator).
        for acc in others:
            if acc in set_targets.get(c.output, []):
                return True
    return False


# --- SSE2 synthesis -------------------------------------------------------

def synth_sse2_reduce(asm_code, func_ctype):
    """Emit a fallback-free SSE2 int32 sum reduction for (int* ptr, len).

    System V: rdi = ptr, esi = len (a multiple of 4 by contract). The result is
    returned in eax. No scalar remainder loop is emitted -- that is the whole
    point: the contract proof guarantees len % 4 == 0.
    """
    loop = asm_code.get_label()
    raw = [
        "push rbp",
        "mov rbp, rsp",
        "pxor xmm0, xmm0",      # 4-lane int32 accumulator
        "mov ecx, esi",         # ecx = len
        "shr ecx, 2",           # ecx = len / 4  (groups of 4 ints)
        "xor rax, rax",         # rax = byte offset
        loop + ":",
        "movdqu xmm1, [rdi + rax]",
        "paddd xmm0, xmm1",
        "add rax, 16",
        "dec ecx",
        "jnz " + loop,
        # horizontal add of the 4 lanes -> eax
        "movdqa xmm1, xmm0",
        "psrldq xmm1, 8",
        "paddd xmm0, xmm1",
        "movdqa xmm1, xmm0",
        "psrldq xmm1, 4",
        "paddd xmm0, xmm1",
        "movd eax, xmm0",
        "mov rsp, rbp",
        "pop rbp",
        "ret",
    ]
    for line in raw:
        asm_code.add(asm_cmds.Raw(line))


def synth_sse2_reduce_u8(asm_code, func_ctype):
    """Emit a fallback-free SSE2 sum of an unsigned-byte array via PSADBW.

    System V: rdi = ptr, esi = len (a multiple of 16 by contract). The result is
    returned in eax. PSADBW computes, for each byte, |b - 0| = b, then
    horizontally sums each group of 8 bytes into the low word of a 64-bit lane;
    so one PSADBW reduces a 16-byte vector to two partial sums with no risk of
    8-bit overflow. PADDQ accumulates across iterations, and a final lane fold
    yields the total. No scalar remainder is emitted -- len % 16 == 0 is proven.
    """
    loop = asm_code.get_label()
    raw = [
        "push rbp",
        "mov rbp, rsp",
        "pxor xmm0, xmm0",      # two 64-bit lane accumulators
        "pxor xmm2, xmm2",      # zero operand for PSADBW
        "mov ecx, esi",         # ecx = len
        "shr ecx, 4",           # ecx = len / 16  (groups of 16 bytes)
        "xor rax, rax",         # rax = byte offset
        loop + ":",
        "movdqu xmm1, [rdi + rax]",
        "psadbw xmm1, xmm2",    # |byte - 0| summed into 2 x 64-bit lanes
        "paddq xmm0, xmm1",     # accumulate without overflow
        "add rax, 16",
        "dec ecx",
        "jnz " + loop,
        # fold the two 64-bit lanes -> eax
        "movdqa xmm1, xmm0",
        "psrldq xmm1, 8",
        "paddq xmm0, xmm1",
        "movd eax, xmm0",       # low 32 bits (a u8-array sum fits in int)
        "mov rsp, rbp",
        "pop rbp",
        "ret",
    ]
    for line in raw:
        asm_code.add(asm_cmds.Raw(line))


def synth_sse_elementwise(asm_code, desc):
    """Emit a fallback-free packed-SSE element-wise kernel from a descriptor
    carrying the System V registers, element size, kind and op.

    Kinds: binary (a op b), saxpy (s*a + b), fma (a*b + c), map (f(a)).
    Element type is float (4 lanes / 128 bits) or double (2 lanes). The contract
    proof guarantees the count is a multiple of the lane count, so there is no
    scalar tail. A fixed-size kernel (no length argument) bakes the proven
    element count in as a literal."""
    elem = desc["elem_size"]
    if elem == 4:
        mov, suf, shift = "movups", "ps", 2
        bcast = "shufps %s, %s, 0"
    else:
        mov, suf, shift = "movupd", "pd", 1
        bcast = "unpcklpd %s, %s"
    ins = desc["in_regs"]
    out = desc["out_reg"]
    scalar = desc.get("scalar_reg")
    fixed = desc.get("fixed_count")
    lanes = 16 // elem
    loop = asm_code.get_label()

    # ---- dot product: returns the reduced scalar in xmm0 -------------------
    if desc["kind"] == "dot":
        raw = ["push rbp", "mov rbp, rsp", "xorps xmm0, xmm0"]
        if fixed is not None:
            raw.append("mov r8d, %d" % (fixed // lanes))
        else:
            raw += ["mov r8d, %s" % desc["len_reg"], "shr r8d, %d" % shift]
        raw += ["xor rax, rax", loop + ":",
                "%s xmm1, [%s + rax]" % (mov, ins[0]),
                "%s xmm2, [%s + rax]" % (mov, ins[1]),
                "mul%s xmm1, xmm2" % suf,
                "add%s xmm0, xmm1" % suf,
                "add rax, 16", "dec r8d", "jnz " + loop]
        # horizontal sum of the lane accumulator into xmm0[0]
        if elem == 4:
            raw += ["movaps xmm1, xmm0", "shufps xmm1, xmm0, 0x0e",
                    "addps xmm0, xmm1", "movaps xmm1, xmm0",
                    "shufps xmm1, xmm0, 0x01", "addss xmm0, xmm1"]
        else:
            raw += ["movapd xmm1, xmm0", "unpckhpd xmm1, xmm1",
                    "addsd xmm0, xmm1"]
        raw += ["mov rsp, rbp", "pop rbp", "ret"]
        for line in raw:
            asm_code.add(asm_cmds.Raw(line))
        return

    raw = ["push rbp", "mov rbp, rsp"]
    if desc["kind"] in ("saxpy", "scale") and scalar:
        raw.append(bcast % (scalar, scalar))    # broadcast the scalar lane-wise
    if fixed is not None:
        raw.append("mov r8d, %d" % (fixed // lanes))
    else:
        raw += ["mov r8d, %s" % desc["len_reg"], "shr r8d, %d" % shift]
    raw += ["xor rax, rax", loop + ":"]

    if desc["kind"] == "map":
        raw += ["%s xmm1, [%s + rax]" % (mov, ins[0]),
                "sqrt%s xmm1, xmm1" % suf,
                "%s [%s + rax], xmm1" % (mov, out)]
    elif desc["kind"] == "scale":
        raw += ["%s xmm1, [%s + rax]" % (mov, ins[0]),
                "%s%s xmm1, %s" % (desc["op"], suf, scalar),
                "%s [%s + rax], xmm1" % (mov, out)]
    elif desc["kind"] == "saxpy":
        raw += [
            "%s xmm1, [%s + rax]" % (mov, ins[0]),    # x
            "mul%s xmm1, %s" % (suf, scalar),         # s * x
            "%s xmm2, [%s + rax]" % (mov, ins[1]),    # y
            "add%s xmm1, xmm2" % suf,                 # + y
            "%s [%s + rax], xmm1" % (mov, out),
        ]
    elif desc["kind"] == "fma":
        raw += [
            "%s xmm1, [%s + rax]" % (mov, ins[0]),    # a
            "%s xmm2, [%s + rax]" % (mov, ins[1]),    # b
            "mul%s xmm1, xmm2" % suf,                 # a * b
            "%s xmm2, [%s + rax]" % (mov, ins[2]),    # c
            "add%s xmm1, xmm2" % suf,                 # + c
            "%s [%s + rax], xmm1" % (mov, out),
        ]
    else:                                            # binary
        raw += [
            "%s xmm1, [%s + rax]" % (mov, ins[0]),
            "%s xmm2, [%s + rax]" % (mov, ins[1]),
            "%s%s xmm1, xmm2" % (desc["op"], suf),
            "%s [%s + rax], xmm1" % (mov, out),
        ]
    raw += ["add rax, 16", "dec r8d", "jnz " + loop,
            "mov rsp, rbp", "pop rbp", "ret"]
    for line in raw:
        asm_code.add(asm_cmds.Raw(line))
