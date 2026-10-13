"""SPIR-V primitives: writer, reader/validator, reference interpreter.

These run with no compiler involved: modules are built by hand with
shivyc/spirv.py, validated by shivyc/spirv_reader.py, and executed by
tools/spirv_interp.py. The three are written independently of each other, so
agreement between them is evidence, and a disagreement points at one.

    python3 -m unittest tests.test_spirv_prims -v
"""

import os
import struct
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import shivyc.spirv as sp
import shivyc.spirv_reader as rd
import spirv_examples as ex
import spirv_interp as interp


def f32s(values):
    return bytearray(struct.pack("<%df" % len(values), *values))


def u32s(values):
    return bytearray(struct.pack("<%dI" % len(values), *values))


def unf32(buf):
    return list(struct.unpack("<%df" % (len(buf) // 4), bytes(buf)))


def unu32(buf):
    return list(struct.unpack("<%dI" % (len(buf) // 4), bytes(buf)))


def r32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def binop_module(op, unary=False, in_type="u32", out_type="u32"):
    """out[0] = op(in[0], in[1]) over 32-bit words, one invocation."""
    m = sp.Module()
    m.capability("Shader")
    m.memory_model("Logical", "GLSL450")
    u32 = m.t_int(32, False)
    f32 = m.t_float(32)
    types = {"u32": u32, "f32": f32}
    tin = types[in_type]
    tout = types[out_type]
    void = m.t_void()
    c0 = m.const_int(u32, 0)
    c1 = m.const_int(u32, 1)
    a = ex._buffer(m, tin, 0, readonly=True, name="in")
    o = ex._buffer(m, tout, 1, name="out")
    p_in = m.t_pointer("StorageBuffer", tin)
    p_out = m.t_pointer("StorageBuffer", tout)
    fn = m.function(void)
    blk = fn.block()
    x = blk.load(tin, blk.op("OpAccessChain", p_in, a, c0, c0))
    if unary:
        r = blk.op(op, tout, x)
    else:
        y = blk.load(tin, blk.op("OpAccessChain", p_in, a, c0, c1))
        r = blk.op(op, tout, x, y)
    blk.store(blk.op("OpAccessChain", p_out, o, c0, c0), r)
    blk.ret()
    m.entry_point(fn, "main", "GLCompute", [])
    m.local_size(fn, 1)
    return m


def run_binop(op, a, b=None, in_type="u32", out_type="u32", unary=False):
    m = binop_module(op, unary, in_type, out_type)
    if in_type == "f32":
        inbuf = f32s([a] if unary else [a, b])
    else:
        inbuf = u32s([a] if unary else [a, b])
    out = bytearray(4)
    interp.run(m.words(), "main", (1, 1, 1), {0: inbuf, 1: out})
    return unf32(out)[0] if out_type == "f32" else unu32(out)[0]


class TestWriter(unittest.TestCase):
    def test_header_and_bound(self):
        w = ex.build_vecadd().words()
        self.assertEqual(w[0], 0x07230203)
        self.assertEqual(w[1], 0x00010300)
        self.assertEqual(w[4], 0)
        maxid = 0
        for ins in rd.parse(w).insts:
            maxid = max(maxid, ins.result_id)
        self.assertEqual(w[3], maxid + 1)

    def test_string_encoding(self):
        # "main" is four chars: the terminating nul needs a whole extra word.
        self.assertEqual(len(sp.encode_string("main")), 2)
        self.assertEqual(len(sp.encode_string("abc")), 1)
        self.assertEqual(sp.decode_string(sp.encode_string("hello"), 0),
                         ("hello", 2))

    def test_bytes_roundtrip(self):
        w = ex.build_reduce().words()
        self.assertEqual(sp.bytes_to_words(sp.words_to_bytes(w)), w)

    def test_decode_reencode_is_identical(self):
        for name in ex.EXAMPLES:
            w = ex.EXAMPLES[name]().words()
            header, insts = rd.decode(w)
            again = list(header)
            for ins in insts:
                again.extend(ins.words)
            self.assertEqual(again, w, name)

    def test_no_duplicate_decorations(self):
        # The three buffers share one cached runtime-array type; its
        # ArrayStride must be emitted once.
        w = ex.build_vecadd().words()
        strides = 0
        for ins in rd.parse(w).insts:
            if ins.name == "OpDecorate" and ins.operands[1][1] == 6:
                strides = strides + 1
        self.assertEqual(strides, 1)

    def test_types_are_cached(self):
        m = sp.Module()
        self.assertEqual(m.t_int(32, False), m.t_int(32, False))
        self.assertNotEqual(m.t_int(32, False), m.t_int(32, True))
        self.assertNotEqual(m.t_struct([]), m.t_struct([]))   # nominal

    def test_wide_types_request_capabilities(self):
        m = sp.Module()
        m.t_int(64, True)
        m.t_float(64)
        m.t_int(8, False)
        self.assertEqual(sorted(m.capabilities),
                         ["Float64", "Int64", "Int8"])


class TestDisassembler(unittest.TestCase):
    def test_names_and_enums(self):
        text = rd.disassemble(ex.build_vecadd().words())
        self.assertIn("OpCapability Shader", text)
        self.assertIn("OpMemoryModel Logical GLSL450", text)
        self.assertIn('OpEntryPoint GLCompute %vecadd "main"', text)
        self.assertIn("OpTypePointer StorageBuffer", text)
        self.assertIn("OpFAdd", text)


class TestValidatorAccepts(unittest.TestCase):
    def test_examples_validate(self):
        for name in ex.EXAMPLES:
            errs = rd.validate(ex.EXAMPLES[name]().words())
            self.assertEqual(errs, [], name)


class TestValidatorRejects(unittest.TestCase):
    """Mutation tests: each breaks one rule in a valid module and requires the
    validator to notice. A validator that accepts everything passes the
    positive tests above; these are what show it is checking."""

    def assertRejected(self, words, fragment):
        errs = rd.validate(words)
        joined = "\n".join(errs)
        self.assertTrue(errs, "expected an error mentioning %r, got none"
                        % fragment)
        self.assertIn(fragment, joined)

    def test_bad_magic(self):
        w = ex.build_vecadd().words()
        w[0] = 0x12345678
        self.assertRejected(w, "bad magic")

    def test_bound_too_small(self):
        m = ex.build_vecadd()
        w = m.words()
        w[3] = 5
        self.assertRejected(w, "id bound")

    def test_truncated(self):
        w = ex.build_vecadd().words()
        self.assertRejected(w[:-1], "decode")

    def test_missing_local_size(self):
        m = ex.build_vecadd()
        m.sections[sp.SEC_EXECMODE] = []
        self.assertRejected(m.words(), "LocalSize")

    def test_missing_shader_capability(self):
        m = ex.build_vecadd()
        m.sections[sp.SEC_CAPABILITY] = []
        self.assertRejected(m.words(), "no OpCapability")

    def test_glcompute_without_shader(self):
        m = ex.build_vecadd()
        m.sections[sp.SEC_CAPABILITY] = []
        m.emit(sp.SEC_CAPABILITY, "OpCapability", 0, 0, sp.CAPABILITY["Int8"])
        self.assertRejected(m.words(), "without capability Shader")

    def test_int64_without_capability(self):
        m = ex.build_vecadd()
        m._type(("int", 64, False), "OpTypeInt", 64, 0)    # bypass t_int
        self.assertRejected(m.words(), "64-bit integer type without "
                                       "capability Int64")

    def test_section_order(self):
        m = ex.build_vecadd()
        m.emit(sp.SEC_CAPABILITY, "OpCapability", 0, 0, 1)
        m.sections[sp.SEC_GLOBALS].insert(0,
                                          m.sections[sp.SEC_CAPABILITY][-1])
        self.assertRejected(m.words(), "out of order")

    def test_use_before_definition_in_block(self):
        m = ex.build_vecadd()
        blk = m.functions[0].blocks[0]
        # Swap the OpFAdd (second to last before store/return) with the
        # first load so it uses values not yet computed.
        i_add = None
        for k in range(len(blk.insts)):
            if blk.insts[k][0] & 0xFFFF == 129:
                i_add = k
        blk.insts[0], blk.insts[i_add] = blk.insts[i_add], blk.insts[0]
        self.assertRejected(m.words(), "before its definition")

    def _if_module(self, use_in_merge):
        """if (x == 0) { v = x + 1 }  merge: [use v]. `v` is defined in one
        arm only, so its block does not dominate the merge block."""
        m = sp.Module()
        m.capability("Shader")
        m.memory_model()
        u32 = m.t_int(32, False)
        boolt = m.t_bool()
        c0 = m.const_int(u32, 0)
        c1 = m.const_int(u32, 1)
        gid = ex._gid_var(m)
        out = ex._buffer(m, u32, 0, name="out")
        p_in = m.t_pointer("Input", u32)
        p_sb = m.t_pointer("StorageBuffer", u32)
        fn = m.function(m.t_void())
        entry = fn.block()
        then = fn.block()
        merge = fn.block()
        x = entry.load(u32, entry.op("OpAccessChain", p_in, gid, c0))
        cond = entry.op("OpIEqual", boolt, x, c0)
        entry.selection_merge(merge.label)
        entry.cond_branch(cond, then.label, merge.label)
        v = then.op("OpIAdd", u32, x, c1)
        then.branch(merge.label)
        if use_in_merge:
            merge.store(merge.op("OpAccessChain", p_sb, out, c0, c0), v)
        merge.ret()
        m.entry_point(fn, "main", "GLCompute", [gid])
        m.local_size(fn, 1)
        return m

    def test_definition_must_dominate_use_across_blocks(self):
        self.assertEqual(rd.validate(self._if_module(False).words()), [])
        self.assertRejected(self._if_module(True).words(),
                            "does not dominate block")

    def test_undefined_id(self):
        m = ex.build_vecadd()
        blk = m.functions[0].blocks[0]
        blk.insts[1][-1] = 999        # an OpLoad's pointer operand
        self.assertRejected(m.words(), "undefined id")

    def _two_buffers_same_binding(self, shared_entry):
        m = sp.Module()
        m.capability("Shader")
        m.memory_model()
        f32 = m.t_float(32)
        u32 = m.t_int(32, False)
        a = ex._buffer(m, f32, 0, name="a")
        b = ex._buffer(m, f32, 0, name="b")
        pty = m.t_pointer("StorageBuffer", f32)
        c0 = m.const_int(u32, 0)
        void = m.t_void()
        kernels = [[a, b]] if shared_entry else [[a], [b]]
        for k, uses in enumerate(kernels):
            fn = m.function(void)
            blk = fn.block()
            for v in uses:
                blk.op("OpAccessChain", pty, v, c0, c0)
            blk.ret()
            m.entry_point(fn, "main%d" % k)
            m.local_size(fn, 1)
        return m.words()

    def test_duplicate_binding(self):
        self.assertRejected(self._two_buffers_same_binding(True),
                            "already used")

    def test_same_binding_in_separate_kernels_is_fine(self):
        self.assertEqual(
            rd.validate(self._two_buffers_same_binding(False)), [])

    def test_missing_array_stride(self):
        m = sp.Module()
        m.capability("Shader")
        m.memory_model()
        f32 = m.t_float(32)
        rt = m.t_runtime_array(f32)              # no ArrayStride
        st = m.t_struct([rt])
        m.decorate(st, "Block")
        m.member_decorate(st, 0, "Offset", 0)
        v = m.global_var(m.t_pointer("StorageBuffer", st), "StorageBuffer")
        m.decorate(v, "DescriptorSet", 0)
        m.decorate(v, "Binding", 0)
        fn = m.function(m.t_void())
        fn.block().ret()
        m.entry_point(fn, "main")
        m.local_size(fn, 1)
        self.assertRejected(m.words(), "no ArrayStride")

    def test_missing_offset_and_block(self):
        m = sp.Module()
        m.capability("Shader")
        m.memory_model()
        f32 = m.t_float(32)
        st = m.t_struct([f32])                   # no Block, no Offset
        v = m.global_var(m.t_pointer("StorageBuffer", st), "StorageBuffer")
        m.decorate(v, "DescriptorSet", 0)
        m.decorate(v, "Binding", 0)
        fn = m.function(m.t_void())
        fn.block().ret()
        m.entry_point(fn, "main")
        m.local_size(fn, 1)
        errs = "\n".join(rd.validate(m.words()))
        self.assertIn("not decorated Block", errs)
        self.assertIn("no Offset", errs)

    def test_misaligned_member(self):
        m = sp.Module()
        m.capability("Shader")
        m.memory_model()
        f32 = m.t_float(32)
        st = m.t_struct([f32, f32])
        m.decorate(st, "Block")
        m.member_decorate(st, 0, "Offset", 0)
        m.member_decorate(st, 1, "Offset", 2)    # overlaps and misaligned
        v = m.global_var(m.t_pointer("StorageBuffer", st), "StorageBuffer")
        m.decorate(v, "DescriptorSet", 0)
        m.decorate(v, "Binding", 0)
        fn = m.function(m.t_void())
        fn.block().ret()
        m.entry_point(fn, "main")
        m.local_size(fn, 1)
        errs = "\n".join(rd.validate(m.words()))
        self.assertIn("not aligned", errs)
        self.assertIn("overlaps", errs)

    def test_merge_not_second_to_last(self):
        # The bug the first version of the saxpy example actually had.
        m = ex.build_saxpy_loop()
        header = m.functions[0].blocks[1]
        merge = [i for i in header.insts if i[0] & 0xFFFF == 246][0]
        header.insts.remove(merge)
        header.insts.insert(0, merge)
        self.assertRejected(m.words(), "second-to-last")

    def test_loop_without_loop_merge(self):
        m = ex.build_saxpy_loop()
        header = m.functions[0].blocks[1]
        header.insts = [i for i in header.insts if i[0] & 0xFFFF != 246]
        errs = "\n".join(rd.validate(m.words()))
        self.assertIn("not a loop header", errs)

    def test_conditional_without_merge(self):
        m = ex.build_reduce()
        entry = m.functions[0].blocks[0]
        entry.insts = [i for i in entry.insts if i[0] & 0xFFFF != 247]
        self.assertRejected(m.words(), "no OpSelectionMerge")

    def test_branch_out_of_construct(self):
        # Make the loop body jump straight to the function exit, bypassing
        # the loop's merge block.
        m = ex.build_reduce()
        f = m.functions[0]
        loop_b = f.blocks[3]
        done = f.blocks[6]
        loop_b.insts[-1] = sp.inst_words("OpBranch", 0, 0, [done.label])
        errs = "\n".join(rd.validate(m.words()))
        self.assertIn("neither its merge block", errs)

    def _nested_loops(self, target):
        """loop h0 { loop h1 { if (c) goto <target>; } } -- target picks
        which merge block the inner selection jumps to."""
        m = sp.Module()
        m.capability("Shader")
        m.memory_model()
        fn = m.function(m.t_void())
        yes = m.const_bool(m.t_bool(), True)
        entry = fn.block()
        h0 = fn.block()
        b0 = fn.block()
        h1 = fn.block()
        b1 = fn.block()
        t1 = fn.block()
        s1 = fn.block()
        c1 = fn.block()
        m1 = fn.block()
        c0 = fn.block()
        m0 = fn.block()
        entry.branch(h0.label)
        h0.loop_merge(m0.label, c0.label)
        h0.branch(b0.label)
        b0.branch(h1.label)
        h1.loop_merge(m1.label, c1.label)
        h1.branch(b1.label)
        b1.selection_merge(s1.label)
        b1.cond_branch(yes, t1.label, s1.label)
        t1.branch({"m1": m1, "m0": m0, "c0": c0}[target].label)
        s1.branch(c1.label)
        c1.branch(h1.label)
        m1.branch(c0.label)
        c0.branch(h0.label)
        m0.ret()
        m.entry_point(fn, "main")
        m.local_size(fn, 1)
        return m.words()

    def test_break_from_a_selection_to_its_loop_merge_is_fine(self):
        self.assertEqual(rd.validate(self._nested_loops("m1")), [])

    def test_break_out_of_two_loops_is_rejected(self):
        self.assertRejected(self._nested_loops("m0"), "neither its merge")

    def test_continue_the_outer_loop_from_the_inner_is_rejected(self):
        self.assertRejected(self._nested_loops("c0"), "neither its merge")

    def test_loop_merge_after_a_break_does_not_belong_to_the_selection(self):
        # for (;;) { if (c) break; ... } -- the merge block is only reachable
        # through the selection, yet it branches onward legally.
        m = sp.Module()
        m.capability("Shader")
        m.memory_model()
        fn = m.function(m.t_void())
        yes = m.const_bool(m.t_bool(), True)
        entry = fn.block()
        h = fn.block()
        b = fn.block()
        t = fn.block()
        s = fn.block()
        c = fn.block()
        mg = fn.block()
        after = fn.block()
        entry.branch(h.label)
        h.loop_merge(mg.label, c.label)
        h.branch(b.label)
        b.selection_merge(s.label)
        b.cond_branch(yes, t.label, s.label)
        t.branch(mg.label)
        s.branch(c.label)
        c.branch(h.label)
        mg.branch(after.label)
        after.ret()
        m.entry_point(fn, "main")
        m.local_size(fn, 1)
        self.assertEqual(rd.validate(m.words()), [])

    def test_block_without_terminator_is_refused_by_the_writer(self):
        m = sp.Module()
        fn = m.function(m.t_void())
        fn.block()
        with self.assertRaises(ValueError):
            m.words()

    def test_recursion(self):
        m = sp.Module()
        m.capability("Shader")
        m.memory_model()
        void = m.t_void()
        helper = m.function(void)
        hb = helper.block()
        hb.op("OpFunctionCall", void, helper.id)
        hb.ret()
        fn = m.function(void)
        b = fn.block()
        b.op("OpFunctionCall", void, helper.id)
        b.ret()
        m.entry_point(fn, "main")
        m.local_size(fn, 1)
        self.assertRejected(m.words(), "recursion")

    def test_phi_missing_predecessor(self):
        m = ex.build_saxpy_loop()
        header = m.functions[0].blocks[1]
        phi = [i for i in header.insts if i[0] & 0xFFFF == 245][0]
        del phi[-2:]                               # drop one (value, parent)
        phi[0] = ((len(phi)) << 16) | 245
        self.assertRejected(m.words(), "OpPhi")


class TestInterpreter(unittest.TestCase):
    def test_vecadd(self):
        n = 200
        a = [float(i) * 0.5 for i in range(n)]
        b = [float(100 - i) for i in range(n)]
        out = bytearray(4 * n)
        # 4 workgroups of 64 = 256 invocations over 200 elements: the extra
        # invocations would be out of range, so size the buffers to 256.
        a += [0.0] * 56
        b += [0.0] * 56
        out = bytearray(4 * 256)
        stats = interp.run(ex.build_vecadd().words(), "main", (4, 1, 1),
                           {0: f32s(a), 1: f32s(b), 2: out})
        got = unf32(out)
        self.assertEqual(got, [r32(x + y) for x, y in zip(a, b)])
        self.assertGreater(stats["steps"], 0)

    def test_out_of_bounds_is_an_error_not_silence(self):
        a = f32s([1.0] * 10)
        with self.assertRaises(interp.InterpError) as cm:
            interp.run(ex.build_vecadd().words(), "main", (1, 1, 1),
                       {0: a, 1: f32s([1.0] * 10), 2: bytearray(40)})
        self.assertIn("out-of-bounds", str(cm.exception))

    def test_saxpy_loop_variable_trip_count(self):
        n = 16
        x = [float(i) + 0.25 for i in range(n)]
        y = [float(i) * 3.0 for i in range(n)]
        k = [i % 5 for i in range(n)]            # 0..4 iterations each
        ybuf = f32s(y)
        interp.run(ex.build_saxpy_loop(16).words(), "main", (1, 1, 1),
                   {0: f32s(x), 1: ybuf, 2: u32s(k)})
        want = []
        for i in range(n):
            acc = y[i]
            for _j in range(k[i]):
                acc = r32(acc + x[i])
            want.append(acc)
        self.assertEqual(unf32(ybuf), want)

    def test_reduce_workgroup_memory_and_barrier(self):
        groups = 4
        size = 8
        vals = [float(i % 7) + 0.5 for i in range(groups * size)]
        out = bytearray(4 * groups)
        interp.run(ex.build_reduce(size).words(), "main", (groups, 1, 1),
                   {0: f32s(vals), 1: out})
        want = []
        for g in range(groups):
            acc = 0.0
            for i in range(size):
                acc = r32(acc + vals[g * size + i])
            want.append(acc)
        self.assertEqual(unf32(out), want)

    def test_divergent_barrier_is_an_error(self):
        m = sp.Module()
        m.capability("Shader")
        m.memory_model()
        u32 = m.t_int(32, False)
        boolt = m.t_bool()
        c0 = m.const_int(u32, 0)
        gid = ex._gid_var(m)
        p_in = m.t_pointer("Input", u32)
        scope = m.const_int(u32, sp.SCOPE["Workgroup"])
        sem = m.const_int(u32, sp.SEM_ACQUIRE_RELEASE
                          | sp.SEM_WORKGROUP_MEMORY)
        fn = m.function(m.t_void())
        entry = fn.block()
        then = fn.block()
        merge = fn.block()
        x = entry.load(u32, entry.op("OpAccessChain", p_in, gid, c0))
        is0 = entry.op("OpIEqual", boolt, x, c0)
        entry.selection_merge(merge.label)
        entry.cond_branch(is0, then.label, merge.label)
        then.op("OpControlBarrier", 0, scope, scope, sem)
        then.branch(merge.label)
        merge.ret()
        m.entry_point(fn, "main", "GLCompute", [gid])
        m.local_size(fn, 4)
        self.assertEqual(rd.validate(m.words()), [])
        with self.assertRaises(interp.InterpError) as cm:
            interp.run(m.words(), "main", (1, 1, 1), {})
        self.assertIn("barrier", str(cm.exception))

    def test_invalid_module_is_refused(self):
        m = ex.build_vecadd()
        m.sections[sp.SEC_EXECMODE] = []
        with self.assertRaises(interp.InterpError) as cm:
            interp.run(m.words(), "main", (1, 1, 1), {})
        self.assertIn("does not validate", str(cm.exception))

    def test_nonwritable_store_is_an_error(self):
        m = sp.Module()
        m.capability("Shader")
        m.memory_model()
        f32 = m.t_float(32)
        u32 = m.t_int(32, False)
        c0 = m.const_int(u32, 0)
        a = ex._buffer(m, f32, 0, readonly=True, name="a")
        fn = m.function(m.t_void())
        b = fn.block()
        p = b.op("OpAccessChain", m.t_pointer("StorageBuffer", f32), a, c0, c0)
        b.store(p, m.const_float(f32, 1.0))
        b.ret()
        m.entry_point(fn, "main")
        m.local_size(fn, 1)
        with self.assertRaises(interp.InterpError) as cm:
            interp.run(m.words(), "main", (1, 1, 1), {0: f32s([0.0])})
        self.assertIn("NonWritable", str(cm.exception))


class TestIntegerSemantics(unittest.TestCase):
    """The interpreter's integer ops against C's definitions. These are the
    cases the back end's lowering will have to get right, so they are pinned
    here first."""

    def s(self, x):
        return x & 0xFFFFFFFF

    def test_wraparound(self):
        self.assertEqual(run_binop("OpIAdd", 0xFFFFFFFF, 1), 0)
        self.assertEqual(run_binop("OpISub", 0, 1), 0xFFFFFFFF)
        self.assertEqual(run_binop("OpIMul", 0x10000, 0x10000), 0)

    def test_signed_division_truncates_toward_zero(self):
        # C: -7 / 2 == -3 and -7 % 2 == -1 (remainder takes the dividend's
        # sign). SMod is the *other* convention: the divisor's sign.
        self.assertEqual(run_binop("OpSDiv", self.s(-7), 2), self.s(-3))
        self.assertEqual(run_binop("OpSRem", self.s(-7), 2), self.s(-1))
        self.assertEqual(run_binop("OpSRem", 7, self.s(-2)), 1)
        self.assertEqual(run_binop("OpSMod", self.s(-7), 2), 1)
        self.assertEqual(run_binop("OpSMod", 7, self.s(-2)), self.s(-1))

    def test_unsigned_vs_signed_compare(self):
        big = 0x80000000                  # INT_MIN as a signed value
        self.assertEqual(run_bool("OpULessThan", 1, big), 1)
        self.assertEqual(run_bool("OpSLessThan", 1, big), 0)
        self.assertEqual(run_bool("OpSLessThan", big, 1), 1)

    def test_arithmetic_shift_keeps_the_sign(self):
        self.assertEqual(run_binop("OpShiftRightArithmetic",
                                   self.s(-8), 1), self.s(-4))
        self.assertEqual(run_binop("OpShiftRightLogical",
                                   self.s(-8), 1), 0x7FFFFFFC)

    def test_division_by_zero_is_undefined(self):
        for op in ("OpUDiv", "OpSDiv", "OpUMod", "OpSRem", "OpSMod"):
            with self.assertRaises(interp.InterpError, msg=op):
                run_binop(op, 5, 0)

    def test_shift_count_at_width_is_undefined(self):
        self.assertEqual(run_binop("OpShiftLeftLogical", 1, 31), 0x80000000)
        for op in ("OpShiftLeftLogical", "OpShiftRightLogical",
                   "OpShiftRightArithmetic"):
            with self.assertRaises(interp.InterpError, msg=op):
                run_binop(op, 1, 32)

    def test_float_to_int_overflow_and_nan_are_undefined(self):
        self.assertEqual(run_binop("OpConvertFToS", -3.9, in_type="f32",
                                   unary=True), self.s(-3))
        for bad in (float("nan"), 3.0e9, float("inf")):
            with self.assertRaises(interp.InterpError):
                run_binop("OpConvertFToS", bad, in_type="f32", unary=True)
        with self.assertRaises(interp.InterpError):
            run_binop("OpConvertFToU", -1.0, in_type="f32", unary=True)


def run_bool(op, a, b):
    """A comparison, returned as 0/1 by selecting between two constants."""
    m = sp.Module()
    m.capability("Shader")
    m.memory_model("Logical", "GLSL450")
    u32 = m.t_int(32, False)
    boolt = m.t_bool()
    c0 = m.const_int(u32, 0)
    c1 = m.const_int(u32, 1)
    ina = ex._buffer(m, u32, 0, readonly=True, name="in")
    out = ex._buffer(m, u32, 1, name="out")
    p = m.t_pointer("StorageBuffer", u32)
    fn = m.function(m.t_void())
    blk = fn.block()
    x = blk.load(u32, blk.op("OpAccessChain", p, ina, c0, c0))
    y = blk.load(u32, blk.op("OpAccessChain", p, ina, c0, c1))
    r = blk.op(op, boolt, x, y)
    sel = blk.op("OpSelect", u32, r, c1, c0)
    blk.store(blk.op("OpAccessChain", p, out, c0, c0), sel)
    blk.ret()
    m.entry_point(fn, "main")
    m.local_size(fn, 1)
    o = bytearray(4)
    interp.run(m.words(), "main", (1, 1, 1), {0: u32s([a, b]), 1: o})
    return unu32(o)[0]


class TestFloatSemantics(unittest.TestCase):
    def test_f32_rounding_after_each_op(self):
        # 16777216 + 1 is not representable in binary32.
        self.assertEqual(run_binop("OpFAdd", 16777216.0, 1.0, "f32", "f32"),
                         16777216.0)

    def test_nan_comparisons(self):
        nan = float("nan")
        # Ordered != is false for NaN; unordered != is true.
        for op, want in (("OpFOrdEqual", 0), ("OpFOrdNotEqual", 0),
                         ("OpFUnordNotEqual", 1), ("OpFOrdLessThan", 0),
                         ("OpFUnordLessThan", 1)):
            self.assertEqual(float_cmp(op, nan, 1.0), want, op)
        self.assertEqual(float_cmp("OpFOrdLessThan", 1.0, 2.0), 1)

    def test_negative_zero(self):
        out = run_binop("OpFNegate", 0.0, None, "f32", "f32", unary=True)
        self.assertEqual(str(out), "-0.0")


def float_cmp(op, a, b):
    m = sp.Module()
    m.capability("Shader")
    m.memory_model("Logical", "GLSL450")
    u32 = m.t_int(32, False)
    f32 = m.t_float(32)
    boolt = m.t_bool()
    c0 = m.const_int(u32, 0)
    c1 = m.const_int(u32, 1)
    ina = ex._buffer(m, f32, 0, readonly=True, name="in")
    out = ex._buffer(m, u32, 1, name="out")
    pf = m.t_pointer("StorageBuffer", f32)
    pu = m.t_pointer("StorageBuffer", u32)
    fn = m.function(m.t_void())
    blk = fn.block()
    x = blk.load(f32, blk.op("OpAccessChain", pf, ina, c0, c0))
    y = blk.load(f32, blk.op("OpAccessChain", pf, ina, c0, c1))
    r = blk.op(op, boolt, x, y)
    sel = blk.op("OpSelect", u32, r, c1, c0)
    blk.store(blk.op("OpAccessChain", pu, out, c0, c0), sel)
    blk.ret()
    m.entry_point(fn, "main")
    m.local_size(fn, 1)
    o = bytearray(4)
    interp.run(m.words(), "main", (1, 1, 1), {0: f32s([a, b]), 1: o})
    return unu32(o)[0]


if __name__ == "__main__":
    unittest.main()
