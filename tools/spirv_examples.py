"""Hand-built SPIR-V compute modules, written against shivyc/spirv.py.

These exist before the compiler does: they are the first inhabitants of the
back end's test corpus, they exercise the writer, the validator and the
interpreter against each other, and they are the shape of module the IL
lowering will emit -- a storage-buffer block per buffer parameter, explicit
Offset / ArrayStride, GlobalInvocationId for the thread index, structured
control flow.

    vecadd      c[i] = a[i] + b[i]
    saxpy_loop  y[i] += a * x[i], with the multiply done by a counted loop
                (OpLoopMerge, OpPhi) -- structured control flow
    reduce      per-workgroup sum through Workgroup memory and barriers
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

import shivyc.spirv as sp


def _buffer(m, elem, binding, readonly=False, name=None):
    """A storage buffer holding one runtime array of `elem` (4 bytes)."""
    rt = m.t_runtime_array(elem)
    m.decorate(rt, "ArrayStride", 4)
    st = m.t_struct([rt])
    m.decorate(st, "Block")
    m.member_decorate(st, 0, "Offset", 0)
    if readonly:
        m.member_decorate(st, 0, "NonWritable")
    ptr = m.t_pointer("StorageBuffer", st)
    var = m.global_var(ptr, "StorageBuffer")
    m.decorate(var, "DescriptorSet", 0)
    m.decorate(var, "Binding", binding)
    if name:
        m.name(var, name)
        m.name(st, name + "_t")
    return var


def _gid_var(m):
    u32 = m.t_int(32, False)
    v3 = m.t_vector(u32, 3)
    ptr = m.t_pointer("Input", v3)
    var = m.global_var(ptr, "Input")
    m.decorate(var, "BuiltIn", sp.BUILTIN["GlobalInvocationId"])
    m.name(var, "gl_GlobalInvocationID")
    return var


def build_vecadd(local_size=64):
    m = sp.Module()
    m.capability("Shader")
    m.memory_model("Logical", "GLSL450")
    f32 = m.t_float(32)
    u32 = m.t_int(32, False)
    void = m.t_void()
    c0 = m.const_int(u32, 0)
    a = _buffer(m, f32, 0, readonly=True, name="a")
    b = _buffer(m, f32, 1, readonly=True, name="b")
    c = _buffer(m, f32, 2, name="c")
    gid = _gid_var(m)
    p_in_u32 = m.t_pointer("Input", u32)
    p_sb_f32 = m.t_pointer("StorageBuffer", f32)
    fn = m.function(void)
    m.name(fn.id, "vecadd")
    blk = fn.block()
    xp = blk.op("OpAccessChain", p_in_u32, gid, c0)
    i = blk.load(u32, xp)
    pa = blk.op("OpAccessChain", p_sb_f32, a, c0, i)
    pb = blk.op("OpAccessChain", p_sb_f32, b, c0, i)
    pc = blk.op("OpAccessChain", p_sb_f32, c, c0, i)
    s = blk.op("OpFAdd", f32, blk.load(f32, pa), blk.load(f32, pb))
    blk.store(pc, s)
    blk.ret()
    m.entry_point(fn, "main", "GLCompute", [gid])
    m.local_size(fn, local_size)
    return m


def build_saxpy_loop(local_size=64):
    """y[i] = y[i] + a * x[i], where a (an int in buffer 2) is applied by
    repeated addition: a structured loop with a phi for the counter and one
    for the accumulator. The bound is data, so the loop runs a variable
    number of times per invocation."""
    m = sp.Module()
    m.capability("Shader")
    m.memory_model("Logical", "GLSL450")
    f32 = m.t_float(32)
    u32 = m.t_int(32, False)
    boolt = m.t_bool()
    void = m.t_void()
    c0 = m.const_int(u32, 0)
    c1 = m.const_int(u32, 1)
    x = _buffer(m, f32, 0, readonly=True, name="x")
    y = _buffer(m, f32, 1, name="y")
    k = _buffer(m, u32, 2, readonly=True, name="k")
    gid = _gid_var(m)
    p_in_u32 = m.t_pointer("Input", u32)
    p_sb_f32 = m.t_pointer("StorageBuffer", f32)
    p_sb_u32 = m.t_pointer("StorageBuffer", u32)
    fn = m.function(void)
    m.name(fn.id, "saxpy_loop")
    entry = fn.block()
    header = fn.block()
    body = fn.block()
    cont = fn.block()
    merge = fn.block()

    xp = entry.op("OpAccessChain", p_in_u32, gid, c0)
    i = entry.load(u32, xp)
    px = entry.op("OpAccessChain", p_sb_f32, x, c0, i)
    py = entry.op("OpAccessChain", p_sb_f32, y, c0, i)
    pk = entry.op("OpAccessChain", p_sb_u32, k, c0, i)
    xv = entry.load(f32, px)
    yv = entry.load(f32, py)
    kv = entry.load(u32, pk)
    entry.branch(header.label)

    # Phis refer to values defined later (the next iteration's), so their
    # ids are reserved first and the instructions filled in after.
    n_next = m.new_id()
    acc_next = m.new_id()
    n = header.phi(u32, [(c0, entry.label), (n_next, cont.label)])
    acc = header.phi(f32, [(yv, entry.label), (acc_next, cont.label)])
    more = header.op("OpULessThan", boolt, n, kv)
    header.loop_merge(merge.label, cont.label)
    header.cond_branch(more, body.label, merge.label)

    body.branch(cont.label)

    # cont: acc' = acc + x ; n' = n + 1. The reserved ids are used as the
    # results by emitting with an explicit id.
    cont.insts.append(sp.inst_words("OpFAdd", f32, acc_next, [acc, xv]))
    cont.insts.append(sp.inst_words("OpIAdd", u32, n_next, [n, c1]))
    cont.branch(header.label)

    merge.store(py, acc)
    merge.ret()
    m.entry_point(fn, "main", "GLCompute", [gid])
    m.local_size(fn, local_size)
    return m


def build_reduce(local_size=8):
    """Each workgroup sums its `local_size` inputs into out[workgroup]:
    every invocation writes its element to shared memory, barrier, then
    invocation 0 adds them up. (A tree reduction is the obvious next step;
    this one keeps the module small and the barrier semantics visible.)"""
    m = sp.Module()
    m.capability("Shader")
    m.memory_model("Logical", "GLSL450")
    f32 = m.t_float(32)
    u32 = m.t_int(32, False)
    boolt = m.t_bool()
    void = m.t_void()
    c0 = m.const_int(u32, 0)
    c1 = m.const_int(u32, 1)
    cn = m.const_int(u32, local_size)
    inp = _buffer(m, f32, 0, readonly=True, name="inp")
    out = _buffer(m, f32, 1, name="out")
    gid = _gid_var(m)
    lid_t = m.t_pointer("Input", m.t_vector(u32, 3))
    lid = m.global_var(lid_t, "Input")
    m.decorate(lid, "BuiltIn", sp.BUILTIN["LocalInvocationId"])
    wid = m.global_var(lid_t, "Input")
    m.decorate(wid, "BuiltIn", sp.BUILTIN["WorkgroupId"])
    arr = m.t_array(f32, cn)
    p_wg_arr = m.t_pointer("Workgroup", arr)
    shared = m.global_var(p_wg_arr, "Workgroup")
    m.name(shared, "tile")
    p_wg_f32 = m.t_pointer("Workgroup", f32)
    p_in_u32 = m.t_pointer("Input", u32)
    p_sb_f32 = m.t_pointer("StorageBuffer", f32)
    scope_wg = m.const_int(u32, sp.SCOPE["Workgroup"])
    sem = m.const_int(u32, sp.SEM_ACQUIRE_RELEASE | sp.SEM_WORKGROUP_MEMORY)
    fzero = m.const_float(f32, 0.0)
    fn = m.function(void)
    m.name(fn.id, "reduce")
    entry = fn.block()
    then = fn.block()
    loop_h = fn.block()
    loop_b = fn.block()
    loop_c = fn.block()
    loop_m = fn.block()
    done = fn.block()

    gx = entry.load(u32, entry.op("OpAccessChain", p_in_u32, gid, c0))
    lx = entry.load(u32, entry.op("OpAccessChain", p_in_u32, lid, c0))
    wx = entry.load(u32, entry.op("OpAccessChain", p_in_u32, wid, c0))
    v = entry.load(f32, entry.op("OpAccessChain", p_sb_f32, inp, c0, gx))
    entry.store(entry.op("OpAccessChain", p_wg_f32, shared, lx), v)
    entry.op("OpControlBarrier", 0, scope_wg, scope_wg, sem)
    is0 = entry.op("OpIEqual", boolt, lx, c0)
    entry.selection_merge(done.label)
    entry.cond_branch(is0, then.label, done.label)

    then.branch(loop_h.label)

    i_next = m.new_id()
    s_next = m.new_id()
    i = loop_h.phi(u32, [(c0, then.label), (i_next, loop_c.label)])
    s = loop_h.phi(f32, [(fzero, then.label), (s_next, loop_c.label)])
    lt = loop_h.op("OpULessThan", boolt, i, cn)
    loop_h.loop_merge(loop_m.label, loop_c.label)
    loop_h.cond_branch(lt, loop_b.label, loop_m.label)

    t = loop_b.load(f32, loop_b.op("OpAccessChain", p_wg_f32, shared, i))
    s2 = loop_b.op("OpFAdd", f32, s, t)
    loop_b.branch(loop_c.label)

    loop_c.insts.append(sp.inst_words("OpIAdd", u32, i_next, [i, c1]))
    # s_next must be defined where it is used as a phi operand: it flows
    # from loop_c, but s2 is computed in loop_b which dominates loop_c.
    loop_c.insts.append(sp.inst_words("OpCopyObject", f32, s_next, [s2]))
    loop_c.branch(loop_h.label)

    loop_m.store(loop_m.op("OpAccessChain", p_sb_f32, out, c0, wx), s)
    loop_m.branch(done.label)

    done.ret()
    m.entry_point(fn, "main", "GLCompute", [gid, lid, wid])
    m.local_size(fn, local_size)
    return m


EXAMPLES = {
    "vecadd": build_vecadd,
    "saxpy_loop": build_saxpy_loop,
    "reduce": build_reduce,
}


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "vecadd"
    out = sys.argv[2] if len(sys.argv) > 2 else name + ".spv"
    data = EXAMPLES[name]().bytes()
    f = open(out, "wb")
    f.write(data)
    f.close()
    print("wrote %s (%d bytes)" % (out, len(data)))
