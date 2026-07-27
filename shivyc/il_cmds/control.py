"""IL commands for labels, jumps, and function calls."""

import shivyc.asm_cmds as asm_cmds
import shivyc.spots as spots
from shivyc.il_cmds.base import ILCommand

from typing import TYPE_CHECKING
if TYPE_CHECKING:                       # type-only; no runtime import cycle
    from shivyc.il_gen import ILValue
from shivyc.spots import LiteralSpot, MemSpot, RegSpot


def _emit_parallel_int_moves(moves, asm_code):
    """Emit (dest_reg, src_spot, size) integer moves in an order where no move
    overwrites a register another move still needs to read, breaking cycles via
    a scratch register (r11/r10/rax, whichever is free of the operands)."""
    pending = [(d, s, sz) for (d, s, sz) in moves if d != s]
    # Deduplicate identical moves.
    seen, uniq = set(), []
    for m in pending:
        key = (m[0].asm_str(8), m[1].asm_str(m[2]), m[2])
        if key not in seen:
            seen.add(key)
            uniq.append(m)
    pending = uniq

    def is_reg(spot):
        return isinstance(spot, spots.RegSpot)

    guard = 0
    while pending:
        guard += 1
        if guard > 10000:
            raise NotImplementedError("call: argument move scheduling failed")
        ready = None
        for m in pending:
            dest = m[0]
            if not any(o is not m and o[1] == dest for o in pending):
                ready = m
                break
        if ready is not None:
            asm_code.add(asm_cmds.Mov(ready[0], ready[1], ready[2]))
            pending.remove(ready)
            continue
        # A cycle remains (each dest is another move's source, all registers).
        # Relocate one source into a free scratch register, then redirect reads.
        used = {d.asm_str(8) for d, _, _ in pending}
        used |= {s.asm_str(8) for _, s, _ in pending if is_reg(s)}
        temp = None
        for cand in (spots.R11, spots.R10, spots.RAX):
            if cand.asm_str(8) not in used:
                temp = cand
                break
        if temp is None:
            raise NotImplementedError(
                "call: no scratch register to break an argument move cycle")
        src0 = pending[0][1]
        asm_code.add(asm_cmds.Mov(temp, src0, 8))
        pending = [(d, (temp if s == src0 else s), sz)
                   for (d, s, sz) in pending]


class Label(ILCommand):
    """Label - Analogous to an ASM label."""

    def __init__(self, label): # noqa D102
        """The label argument is an string label name unique to this label."""
        self.label = label

    def inputs(self): # noqa D102
        return []

    def outputs(self): # noqa D102
        return []

    def label_name(self):  # noqa D102
        return self.label

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"): # noqa D102
        asm_code.add(asm_cmds.AsmLabel(self.label))


class Jump(ILCommand):
    """Jumps unconditionally to a label."""

    def __init__(self, label): # noqa D102
        self.label = label

    def inputs(self): # noqa D102
        return []

    def outputs(self): # noqa D102
        return []

    def targets(self): # noqa D102
        return [self.label]

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"): # noqa D102
        asm_code.add(asm_cmds.Jmp(self.label))


class _GeneralJump(ILCommand):
    """General class for jumping to a label based on condition."""
    cond: "ILValue"

    # ASM command to output for this jump IL command.
    # (asm_cmds.Je for JumpZero and asm_cmds.Jne for JumpNotZero)
    asm_cmd = None

    def __init__(self, cond, label): # noqa D102
        self.cond = cond
        self.label = label

    def inputs(self): # noqa D102
        return [self.cond]

    def outputs(self): # noqa D102
        return []

    def targets(self): # noqa D102
        return [self.label]

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"): # noqa D102
        size = self.cond.ctype.size

        if isinstance(spotmap[self.cond], LiteralSpot):
            r = get_reg()
            asm_code.add(asm_cmds.Mov(r, spotmap[self.cond], size))
            cond_spot = r
        else:
            cond_spot = spotmap[self.cond]

        zero_spot = LiteralSpot("0")
        asm_code.add(asm_cmds.Cmp(cond_spot, zero_spot, size))
        asm_code.add(self.command(self.label))


class JumpZero(_GeneralJump):
    """Jumps to a label if given condition is zero."""

    command = asm_cmds.Je


class JumpNotZero(_GeneralJump):
    """Jumps to a label if given condition is zero."""

    command = asm_cmds.Jne


def _eightbyte_width(n):
    """Round a partial eightbyte up to the register width that covers it."""
    for w in (1, 2, 4, 8):
        if n <= w:
            return w
    return 8


class Return(ILCommand):
    """RETURN - returns the given value from function.

    If arg is None, then returns from the function without putting any value
    in the return register. Today, only supports values that fit in one
    register.
    """
    arg: "ILValue"

    def __init__(self, arg=None): # noqa D102
        # arg must already be cast to return type
        self.arg = arg

    def inputs(self): # noqa D102
        return [self.arg]

    def outputs(self): # noqa D102
        return []

    def clobber(self):  # noqa D102
        return [spots.RAX]

    def abs_spot_pref(self):  # noqa D102
        # A large struct return lives in memory (RAX:RDX), so do not pin the
        # value to RAX; only a register-sized return value prefers RAX.
        if self.arg is not None and self.arg.ctype.size > 8:
            return {}
        return {self.arg: [spots.RAX]}

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"): # noqa D102
        if self.arg and self.arg.ctype.is_floating():
            size = self.arg.ctype.size
            mov = asm_cmds.Movss if size == 4 else asm_cmds.Movsd
            if spotmap[self.arg] != spots.XMM0:
                asm_code.add(mov(spots.XMM0, spotmap[self.arg], size))
        elif (self.arg and self.arg.ctype.is_struct_union()
              and self.arg.ctype.size > 8):
            # SysV AMD64: a struct of 9..16 bytes (INTEGER class) is returned
            # in RAX:RDX. The low eightbyte goes in RAX, the high one in RDX.
            size = self.arg.ctype.size
            src = spotmap[self.arg]
            if size > 16 or not isinstance(src, MemSpot):
                raise NotImplementedError(
                    "struct return larger than 16 bytes is not supported")
            # The high eightbyte is a partial one for sizes like 11 or 14.
            # SysV returns the whole eightbyte regardless, so widen rather
            # than refuse: the source is a stack slot, which is allocated and
            # aligned in eightbyte units, so reading the full width cannot
            # cross out of the frame.
            hi = _eightbyte_width(size - 8)
            asm_code.add(asm_cmds.Mov(spots.RAX, src, 8))
            asm_code.add(asm_cmds.Mov(spots.RDX, src.shift(8), hi))
        elif self.arg and spotmap[self.arg] != spots.RAX:
            size = self.arg.ctype.size
            src = spotmap[self.arg]
            if (self.arg.ctype.is_struct_union()
                    and size not in (1, 2, 4, 8)):
                # A struct of 3, 5, 6 or 7 bytes has no matching register
                # width, so the move is widened to the eightbyte SysV returns
                # it in. Safe from either kind of source: a register already
                # holds the whole value, and a stack slot is allocated and
                # aligned in eightbyte units. The extra bytes are padding the
                # caller must ignore anyway.
                #
                # Without this the backend raised `unexpected register size`
                # from inside register naming -- a crash rather than a
                # diagnostic, hit by any function returning such a struct.
                size = 8
            asm_code.add(asm_cmds.Mov(spots.RAX, src, size))

        # A frameless function set up no rbp frame, so there is nothing to
        # tear down before returning.
        if not getattr(asm_code, "frameless", False):
            asm_code.add(asm_cmds.Mov(spots.RSP, spots.RBP, 8))
            asm_code.add(asm_cmds.Pop(spots.RBP, None, 8))

        # Metamorphic return: jump through the writable .text slot the caller
        # patched, instead of popping a return address off the stack.
        slot = getattr(asm_code, "metamorphic_current", None)
        if slot:
            asm_code.add(asm_cmds.Raw("jmp QWORD PTR [rip + %s]" % slot))
        else:
            asm_code.add(asm_cmds.Ret())


class Call(ILCommand):
    """Call a given function.

    func - Pointer to function
    args - Arguments of the function, in left-to-right order. Must match the
    parameter types the function expects.
    ret - If function has non-void return type, IL value to save the return
    value. Its type must match the function return value.
    """
    func: "ILValue"
    ret: "ILValue"

    arg_regs = [spots.RDI, spots.RSI, spots.RDX, spots.RCX, spots.R8, spots.R9]

    def __init__(self, func, args, ret): # noqa D102
        self.func = func
        self.args = args
        self.ret = ret
        self.void_return = self.func.ctype.arg.ret.is_void()

        # Set by the stackless-calls pass (shivyc/stackless.py):
        #   direct_name - name of a statically known callee (direct `call`)
        #   tail        - True if this call is in tail position (`jmp`)
        self.direct_name = None
        self.tail = False
        # Set by FuncCall: a variadic callee receives all args on the stack.
        self.variadic = False
        # Set by the pack-args pass (shivyc/pack_args.py): (regs, fields) when
        # this direct call uses the bit-packed calling convention.
        self.pack = None

        # Arguments beyond the sixth are passed on the stack (SysV AMD64).

    def inputs(self): # noqa D102
        # For a direct call the function pointer is never materialized, so it
        # is not a register input.
        if self.direct_name:
            return list(self.args)
        return [self.func] + self.args

    def outputs(self): # noqa D102
        return [] if self.void_return else [self.ret]

    def clobber(self): # noqa D102
        # All caller-saved registers are clobbered by function call
        return [spots.RAX, spots.RCX, spots.RDX, spots.RSI, spots.RDI,
                spots.R8, spots.R9, spots.R10, spots.R11]

    def abs_spot_pref(self): # noqa D102
        prefs = {}
        if not self.void_return and self.ret.ctype.size <= 8:
            prefs = {self.ret: [spots.RAX]}
        # For a packed call the arguments are bit-packed into the argument
        # registers from wherever they happen to live, so they get no single
        # argument-register preference (that would only create false pressure).
        if self.pack:
            return prefs
        # Only scalar integer args have a single-GPR preference; floating args
        # travel through xmm registers and structs larger than a register live
        # in memory (loaded into their ABI registers by explicit moves).
        int_args = [a for a in self.args
                    if not a.ctype.is_floating()
                    and not (a.ctype.is_struct_union() and a.ctype.size > 8)]
        for arg, reg in zip(int_args, self.arg_regs):
            prefs[arg] = [reg]

        return prefs

    def abs_spot_conf(self): # noqa D102
        # We don't want the function pointer to be in the same register as
        # an argument will be placed into. (No function pointer for a direct
        # call, so no constraint.)
        if self.direct_name:
            return {}
        return {self.func: self.arg_regs[0:len(self.args)]}

    def indir_write(self): # noqa D102
        return self.args

    def indir_read(self): # noqa D102
        return self.args

    def _emit_pack(self, packed_regs, packed_fields, spotmap, get_reg, asm_code):
        """Build the bit-packed argument registers for a packed call.

        To stay frugal with registers (a packed signature may itself have its
        arguments spread across every GPR), each packed register is assembled in
        a stack slot using a single scratch register: each field is
        zero-extended (which masks it to its own width), shifted to its bit
        offset, and OR-ed into the slot. The argument registers are loaded from
        the slots last, after every argument source has been read, so a source
        that happens to live in an argument register is safe.
        """
        def value_into(tmp, src, size):
            """Place the low `size` bytes of `src` into `tmp`, zero-extended."""
            if isinstance(src, LiteralSpot):
                masked = int(src.value) & ((1 << (size * 8)) - 1)
                asm_code.add(asm_cmds.Mov(tmp, LiteralSpot(str(masked)), 8))
            elif size in (4, 8):
                # A 4-byte move zero-extends into the full 64-bit register.
                asm_code.add(asm_cmds.Mov(tmp, src, size))
            else:
                asm_code.add(asm_cmds.Raw("movzx %s, %s"
                                          % (tmp.asm_str(8), src.asm_str(size))))

        n = len(packed_regs)

        # Fast path (no stack traffic): build each packed register in a scratch
        # register and move it into its argument register last. A call clobbers
        # every caller-saved GPR, so any register not holding an argument source
        # holds nothing live across the call and is free to use as scratch. We
        # also keep scratch registers off the packed target registers so the
        # final moves never alias. When enough such registers exist (the common
        # case -- few packed registers, arguments not filling the whole file)
        # this avoids the rsp adjustment and memory OR traffic of the fallback.
        gprs = [spots.RAX, spots.RDI, spots.RSI, spots.RDX, spots.RCX,
                spots.R8, spots.R9, spots.R10, spots.R11]
        src_regs = {spotmap[self.args[f.arg_index]] for f in packed_fields
                    if isinstance(spotmap[self.args[f.arg_index]], RegSpot)}
        free = [g for g in gprs
                if g not in src_regs and g not in packed_regs]
        if len(free) >= n + 1:
            work = free[0]
            accs = free[1:1 + n]
            for ri in range(n):
                asm_code.add(asm_cmds.Mov(accs[ri], LiteralSpot("0"), 8))
                for f in (f for f in packed_fields if f.reg_index == ri):
                    value_into(work, spotmap[self.args[f.arg_index]], f.size)
                    if f.bit_offset:
                        asm_code.add(asm_cmds.Raw(
                            "shl %s, %d" % (work.asm_str(8), f.bit_offset)))
                    asm_code.add(asm_cmds.Raw(
                        "or %s, %s" % (accs[ri].asm_str(8), work.asm_str(8))))
            # All argument sources have been read, so writing the packed
            # registers (which may have held a source) is now safe.
            for ri, reg in enumerate(packed_regs):
                asm_code.add(asm_cmds.Mov(reg, accs[ri], 8))
            return

        # Fallback (register-starved case, e.g. arguments occupy nearly the
        # whole register file): build the packed registers in stack slots using
        # RAX as a fixed scratch. RAX is caller-saved (clobbered by the call
        # regardless) and never a packed target; if it holds a source, preserve
        # that value to a stack slot and read it back from there.
        scratch = spots.RAX
        need_save = any(spotmap[self.args[f.arg_index]] == scratch
                        for f in packed_fields)
        total = n + (1 if need_save else 0)
        save_off = 8 * n  # only used when need_save

        asm_code.add(asm_cmds.Sub(spots.RSP, LiteralSpot(str(8 * total)), 8))
        if need_save:
            asm_code.add(asm_cmds.Mov(MemSpot(spots.RSP, save_off), scratch, 8))
        for ri in range(n):
            asm_code.add(asm_cmds.Raw("mov QWORD PTR [rsp+%d], 0" % (8 * ri)))
        for f in packed_fields:
            src = spotmap[self.args[f.arg_index]]
            if src == scratch:
                src = MemSpot(spots.RSP, save_off)
            value_into(scratch, src, f.size)
            if f.bit_offset:
                asm_code.add(asm_cmds.Raw("shl %s, %d"
                                          % (scratch.asm_str(8), f.bit_offset)))
            asm_code.add(asm_cmds.Raw("or QWORD PTR [rsp+%d], %s"
                                      % (8 * f.reg_index, scratch.asm_str(8))))
        for ri, reg in enumerate(packed_regs):
            asm_code.add(asm_cmds.Mov(reg, MemSpot(spots.RSP, 8 * ri), 8))
        asm_code.add(asm_cmds.AsmAdd(spots.RSP, LiteralSpot(str(8 * total)), 8))

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"): # noqa D102
        ret_size = self.func.ctype.arg.ret.size
        ret_float = self.func.ctype.arg.ret.is_floating()

        # Classify register arguments into the two SysV sequences: integer
        # args fill rdi,rsi,rdx,rcx,r8,r9; floating args fill xmm0-7; counted
        # independently. Anything that doesn't fit (or every arg, for a
        # variadic callee) is passed on the stack.
        xmm_regs = spots.xmm_arg_regs
        int_moves, flt_moves, stack_args = [], [], []
        struct_reg_moves = []  # (list-of-regs, struct arg) for 9..16-byte args
        variadic_xmm_count = 0
        if self.pack:
            # Packed calling convention: arguments are bit-packed into the
            # registers named by the plan; nothing is passed on the stack and no
            # per-arg register move is classified here (emit_reg_moves is
            # overridden below to build the packed registers).
            pass
        elif self.variadic:
            # ShivyC's own variadic callees read every argument from the stack,
            # so push them all. But a callee using the ordinary SysV ABI (glibc
            # printf/sprintf/snprintf/...) reads the first arguments from the
            # integer/vector registers instead, so ALSO classify them into
            # registers here; the callee reads whichever it expects. (Arguments
            # past the 6 int / 8 xmm registers reach a standard callee only via
            # the overflow area, whose layout differs from ShivyC's all-stack
            # push -- but every glibc variadic call ShivyC makes fits in
            # registers.) AL must give the number of vector registers used.
            stack_args = list(self.args)
            ii = fi = 0
            for arg in self.args:
                if arg.ctype.is_struct_union() and arg.ctype.size > 8:
                    ii += (arg.ctype.size + 7) // 8
                elif arg.ctype.is_floating() and fi < len(xmm_regs):
                    flt_moves.append((xmm_regs[fi], arg))
                    fi += 1
                elif not arg.ctype.is_floating() and ii < len(self.arg_regs):
                    int_moves.append((self.arg_regs[ii], arg))
                    ii += 1
            variadic_xmm_count = fi
        else:
            ii = fi = 0
            for arg in self.args:
                if arg.ctype.is_struct_union() and arg.ctype.size > 8:
                    # SysV: 9..16-byte struct uses two consecutive integer
                    # registers (all-or-nothing); larger structs, or ones that
                    # do not fit, go on the stack as ceil(size/8) eightbytes.
                    n = (arg.ctype.size + 7) // 8
                    if arg.ctype.size <= 16 and ii + n <= len(self.arg_regs):
                        regs = [self.arg_regs[ii + k] for k in range(n)]
                        struct_reg_moves.append((regs, arg))
                        ii += n
                    else:
                        stack_args.append(arg)
                elif arg.ctype.is_floating() and fi < len(xmm_regs):
                    flt_moves.append((xmm_regs[fi], arg))
                    fi += 1
                elif not arg.ctype.is_floating() and ii < len(self.arg_regs):
                    int_moves.append((self.arg_regs[ii], arg))
                    ii += 1
                else:
                    stack_args.append(arg)
        int_regs_used = [r for r, _ in int_moves]
        for regs, _ in struct_reg_moves:
            int_regs_used.extend(regs)
        # Flatten stack arguments into eightbyte push slots (a struct occupies
        # ceil(size/8) of them), low eightbyte first.
        stack_slots = []  # list of MemSpot, in argument order
        for arg in stack_args:
            if arg.ctype.is_struct_union() and arg.ctype.size > 8:
                n = (arg.ctype.size + 7) // 8
                for k in range(n):
                    stack_slots.append(spotmap[arg].shift(8 * k))
            else:
                stack_slots.append(spotmap[arg])
        has_stack_args = len(stack_slots) > 0

        def emit_reg_moves():
            """Move integer args into GPRs and floating args into xmm regs."""
            # Integer argument moves must be ordered so that no move overwrites
            # a register another move still needs to read. With abs_spot_pref
            # most become no-ops, but a "shift" pattern (e.g. passing
            # (const, a, b, c) where a/b/c already sit in the next argument
            # registers) needs real parallel-move scheduling, breaking any
            # cycle through a scratch register.
            moves = [(reg, spotmap[arg], arg.ctype.size)
                     for reg, arg in int_moves if spotmap[arg] != reg]
            _emit_parallel_int_moves(moves, asm_code)
            for regs, arg in struct_reg_moves:
                base = spotmap[arg]
                k = 0
                for reg in regs:
                    remaining = arg.ctype.size - 8 * k
                    chunk = next(cs for cs in (8, 4, 2, 1) if remaining >= cs)
                    asm_code.add(asm_cmds.Mov(reg, base.shift(8 * k), chunk))
                    k = k + 1
            for xreg, arg in flt_moves:
                if spotmap[arg] != xreg:
                    size = arg.ctype.size
                    fmov = asm_cmds.Movss if size == 4 else asm_cmds.Movsd
                    asm_code.add(fmov(xreg, spotmap[arg], size))

        # Packed calling convention (-f-pack-args): build the packed argument
        # registers from the args wherever they live, replacing the per-arg
        # register moves. Packable signatures never have stack/struct/float
        # args, so the rest of the call machinery (tail/ordinary/metamorphic)
        # is reused unchanged. A packed call is always a direct call.
        if self.pack:
            packed_regs, packed_fields = self.pack
            int_regs_used = list(packed_regs)

            def emit_reg_moves():
                self._emit_pack(packed_regs, packed_fields,
                                spotmap, get_reg, asm_code)

        def emit_ret():
            """Move the return value out of its ABI register if needed."""
            if self.void_return:
                return
            ret_ctype = self.func.ctype.arg.ret
            if (ret_ctype.is_struct_union() and ret_size > 8):
                # SysV: a 9..16-byte struct comes back in RAX:RDX. Store both
                # eightbytes into the result's memory home.
                dst = spotmap[self.ret]
                if ret_size > 16 or not isinstance(dst, MemSpot):
                    raise NotImplementedError(
                        "struct return larger than 16 bytes is not supported")
                hi = _eightbyte_width(ret_size - 8)
                asm_code.add(asm_cmds.Mov(dst, spots.RAX, 8))
                asm_code.add(asm_cmds.Mov(dst.shift(8), spots.RDX, hi))
                return
            if ret_float:
                if spotmap[self.ret] != spots.XMM0:
                    fmov = asm_cmds.Movss if ret_size == 4 else asm_cmds.Movsd
                    asm_code.add(fmov(spotmap[self.ret], spots.XMM0, ret_size))
            elif spotmap[self.ret] != spots.RAX:
                # The caller side of the same widening: a struct of 3, 5, 6
                # or 7 bytes comes back in a full eightbyte of RAX, so the
                # move out of it needs a real register width too.
                mov_size = ret_size
                if (self.ret.ctype.is_struct_union()
                        and mov_size not in (1, 2, 4, 8)):
                    mov_size = 8
                asm_code.add(asm_cmds.Mov(spotmap[self.ret], spots.RAX,
                                          mov_size))

        # Push stack arguments (the 7th onward) right-to-left, so the 7th ends
        # up at the lowest address ([rsp] at the call). The function body keeps
        # rsp 16-byte aligned, so an odd number of 8-byte pushes needs 8 bytes
        # of padding to keep the stack 16-aligned at the `call`.
        pad = 8 if (len(stack_slots) % 2 == 1) else 0
        cleanup = pad + 8 * len(stack_slots)
        if pad:
            asm_code.add(asm_cmds.Sub(spots.RSP, LiteralSpot(str(pad)), 8))
        for slot in reversed(stack_slots):
            # x86-64 `push imm` encodes only a 32-bit (sign-extended)
            # immediate, so a wider literal argument cannot be pushed directly.
            # No scratch register is guaranteed free here (the argument-register
            # moves have not happened yet), so push the low half as an 8-byte
            # slot and overwrite its high 4 bytes with a 32-bit store. e.g.
            # passing LLONG_MAX/LLONG_MIN as a stack argument.
            if (isinstance(slot, LiteralSpot)
                    and not (-(2 ** 31) <= int(slot.value) < 2 ** 31)):
                v = int(slot.value) & 0xFFFFFFFFFFFFFFFF
                low = v & 0xFFFFFFFF
                high = (v >> 32) & 0xFFFFFFFF
                low_s = low - (1 << 32) if low >= (1 << 31) else low
                high_s = high - (1 << 32) if high >= (1 << 31) else high
                asm_code.add(asm_cmds.Push(LiteralSpot(str(low_s)), None, 8))
                asm_code.add(asm_cmds.Raw(
                    "mov DWORD PTR [rsp+4], %d" % high_s))
            else:
                asm_code.add(asm_cmds.Push(slot, None, 8))

        # Move arguments into the argument registers (same for all call forms).
        if self.direct_name:
            emit_reg_moves()
            target = self.direct_name
        else:
            func_spot = spotmap[self.func]
            func_size = self.func.ctype.size

            # Check if the function-pointer spot will be clobbered by moving the
            # arguments into the correct (integer) registers -- and, for a
            # variadic call, by the `mov eax, <xmm count>` written below (which
            # zero-extends over all of RAX). In either case relocate the pointer
            # to a safe scratch register before the moves.
            avoid = list(int_regs_used)
            if self.variadic:
                avoid.append(spots.RAX)
            if spotmap[self.func] in avoid:
                r = get_reg([], avoid)
                asm_code.add(asm_cmds.Mov(r, spotmap[self.func], func_size))
                func_spot = r

            emit_reg_moves()
            target = func_spot

        # Variadic call under the standard SysV ABI: AL holds the number of
        # vector registers used to pass floating varargs (glibc printf/sprintf
        # and friends read it before touching the xmm save area). ShivyC's own
        # variadic callees ignore AL, so setting it is always safe.
        if self.variadic:
            asm_code.add(asm_cmds.Mov(spots.RAX,
                                      LiteralSpot(str(variadic_xmm_count)), 4))
        # Metamorphic call: the target returns via a self-modified slot in
        # writable .text rather than the stack. We write our return address
        # into that slot, then jump (not call) into the callee. No return
        # address is pushed. Takes precedence over tail/ordinary forms.
        if (self.direct_name
                and not has_stack_args
                and self.direct_name in getattr(
                    asm_code, "metamorphic_funcs", set())):
            ret_label = asm_code.get_label()
            slot = self.direct_name + "__metaret"
            asm_code.add(asm_cmds.Raw("lea r11, [rip + %s]" % ret_label))
            asm_code.add(asm_cmds.Raw(
                "mov QWORD PTR [rip + %s], r11" % slot))
            asm_code.add(asm_cmds.Raw("jmp " + self.direct_name))
            asm_code.add(asm_cmds.Raw(ret_label + ":"))
            emit_ret()
            return

        if self.tail and not has_stack_args:
            # Tail call: tear down this frame (if any), then jump. The callee's
            # `ret` returns directly to our caller -- no return address for this
            # frame is ever pushed. (self.direct_name is guaranteed set here.)
            if not getattr(asm_code, "frameless", False):
                asm_code.add(asm_cmds.Mov(spots.RSP, spots.RBP, 8))
                asm_code.add(asm_cmds.Pop(spots.RBP, None, 8))
            asm_code.add(asm_cmds.Jmp(self.direct_name))
            return

        # Ordinary (non-tail) call.
        if self.direct_name:
            asm_code.add(asm_cmds.Raw("call " + self.direct_name))
        else:
            # The call target is held in a register as a full machine address,
            # so it is always 64-bit -- even under -f-pointer-compression where
            # the stored function pointer is 4 bytes (it is loaded zero-extended
            # into the 64-bit register before the call).
            asm_code.add(asm_cmds.AsmCall(target, None, 8))

        # Caller cleans up any stack-passed arguments.
        if cleanup:
            asm_code.add(asm_cmds.AsmAdd(spots.RSP, LiteralSpot(str(cleanup)), 8))

        # xmm8-15 are caller-saved, so a call may have clobbered the packed
        # flag register. Inside a hot/interrupt routine, refresh it from the
        # memory mirror so subsequent zero-latency reads stay correct.
        if (getattr(asm_code, "simd_pack_enabled", False)
                and getattr(asm_code, "simd_pack_hot", False)
                and asm_code.simd_pack.active):
            asm_code.simd_pack.emit_refresh(asm_code)

        emit_ret()
