"""IL commands for setting/reading values and getting value addresses."""

import shivyc.asm_cmds as asm_cmds
import shivyc.ctypes as ctypes
import shivyc.spots as spots
from shivyc.il_cmds.base import ILCommand
from shivyc.errors import CompilerError
from shivyc.spots import RegSpot, MemSpot, LiteralSpot

from typing import TYPE_CHECKING
if TYPE_CHECKING:                       # avoids an import cycle at runtime;
    from shivyc.il_gen import ILValue   # the transpiler reads this statically


# Private monotonic counter for the single label Set._set_bool needs. We
# deliberately do NOT use asm_code.get_label() there: get_label() is defined on
# both il_gen.ILCode and asm_gen.ASMCode, and because this module imports il_gen
# (for ILValue) the transpiler treats `asm_code.get_label()` as a cross-module
# virtual call and dispatches it through il_gen's vtable -- a different slot than
# ASMCode's -- so the self-hosted (native) compiler jumped through a garbage
# function pointer and crashed on every conversion to _Bool. A module-local
# counter with its own label prefix is unique, needs no cross-module dispatch,
# and matches the host behaviour exactly.
_set_bool_label_count = 0


class _ValueCmd(ILCommand):
    """Abstract base class for value commands.

    This class defines a helper function for moving data from one location
    to another.
    """
    output: "ILValue"
    arg: "ILValue"
    val: "ILValue"

    def move_data(self, target_spot, start_spot, size, reg, asm_code):
        """Emits code to move data from start to target.

        Given a target spot, start spot, size of data to move,
        and a register that can be clobbered in the move, this function
        emits code to move all the data. It is efficient whether the input
        spots are registers or memory, and in particular this function
        works even if the input size is not in {1, 2, 4, 8}.

        The given register is used as an intermediary for transferring
        values between the target_spot and start_spot. It is *always* safe
        for `reg` to be one of these two, and in fact it is recommended
        that if either of target_spot or start_spot is a register then
        `reg` be equal to that.
        """
        # TODO: consider padding everything to 8 bytes to reduce the
        # number of mov operations emitted for struct copying.
        shift = 0
        while shift < size:
            reg_size = self._reg_size(size - shift)
            # Compute each chunk's spot from the ORIGINAL base by the absolute
            # offset `shift`. (Reassigning start_spot/target_spot here would
            # compound the offset across iterations and corrupt copies larger
            # than 16 bytes, where three or more chunks are emitted.)
            cur_start = start_spot.shift(shift)
            cur_target = target_spot.shift(shift)

            if isinstance(cur_start, LiteralSpot):
                # x86-64 `mov mem, imm` accepts only a 32-bit (sign-extended)
                # immediate. A wider literal written to memory must first be
                # loaded into the scratch register (`mov reg, imm64` assembles
                # as movabs), then stored. e.g. storing PY_SSIZE_T_MAX or
                # LLONG_MIN through a pointer.
                if (reg_size == 8
                        and not isinstance(cur_target, RegSpot)
                        and not (-(2 ** 31) <= cur_start.value < 2 ** 31)):
                    asm_code.add(asm_cmds.Mov(reg, cur_start, reg_size))
                    src = reg
                else:
                    src = cur_start
            else:
                src = reg
                if reg != cur_start:
                    asm_code.add(asm_cmds.Mov(reg, cur_start, reg_size))

            if src != cur_target:
                asm_code.add(asm_cmds.Mov(cur_target, src, reg_size))

            shift += reg_size

    def _reg_size(self, size) -> int:
        """Return largest register size that does not overfit given size."""
        reg_sizes = [8, 4, 2, 1]
        for reg_size in reg_sizes:
            if size >= reg_size:
                return reg_size


class LoadArg(ILCommand):
    """Loads a function argument value into an IL value.

    output is the IL value to load the function argument value into,
    and arg_num is the index of the argument to load. For example,
    at the start of the body of the following function:

       int func(int a, int b);

    the following two LoadArg commands would be appropriate

       LoadArg(a, 0)
       LoadArg(b, 1)

    in order to load the first function argument into the variable a and
    the second function argument into the variable b.
    """
    output: "ILValue"
    arg_regs = [spots.RDI, spots.RSI, spots.RDX, spots.RCX, spots.R8, spots.R9]

    def __init__(self, output, arg_num, all_stack=False, reg=None,
                 is_float=False, stack_index=None):
        self.output = output
        self.arg_num = arg_num
        self.is_float = is_float
        if reg is not None:
            # Explicit register (used by the ABI-aware generation site, which
            # counts integer and floating arguments in separate sequences).
            self.arg_reg = reg
            self.stack_spot = None
        elif stack_index is not None:
            self.arg_reg = None
            self.stack_spot = MemSpot(spots.RBP, 16 + 8 * stack_index)
        elif all_stack or arg_num >= len(self.arg_regs):
            # Variadic functions receive every argument on the stack; ordinary
            # functions only the 7th onward. Stack arguments sit at [rbp+16],
            # [rbp+24], ... (the return address is at [rbp+8]).
            self.arg_reg = None
            stack_index = arg_num if all_stack else arg_num - len(self.arg_regs)
            self.stack_spot = MemSpot(spots.RBP, 16 + 8 * stack_index)
        else:
            self.arg_reg = self.arg_regs[arg_num]
            self.stack_spot = None

    def inputs(self):
        return []

    def outputs(self):
        return [self.output]

    def clobber(self):
        # A floating argument arrives in an xmm register, which the integer
        # allocator does not track, so it need not be reported as clobbered.
        if self.is_float:
            return []
        return [self.arg_reg] if self.arg_reg else []

    def abs_spot_pref(self):
        # Floating outputs live in memory, so an xmm preference is meaningless.
        if self.is_float:
            return {}
        return {self.output: [self.arg_reg]} if self.arg_reg else {}

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"):
        dest = spotmap[self.output]
        size = self.output.ctype.size
        src = self.arg_reg if self.arg_reg else self.stack_spot
        if dest == src:
            return
        # A floating argument arrives in an xmm register (or, when spilled, on
        # the stack); move it with the SSE move into its memory home.
        if self.is_float:
            fmov = asm_cmds.Movss if size == 4 else asm_cmds.Movsd
            if self.arg_reg is not None:
                asm_code.add(fmov(dest, self.arg_reg, size))
            else:
                from shivyc.spots import XMM0
                asm_code.add(fmov(XMM0, src, size))
                asm_code.add(fmov(dest, XMM0, size))
            return
        # A register source can move directly to a register or memory dest.
        # A memory source (stack argument) cannot move directly to a memory
        # dest, so route it through a scratch register in that case.
        if (self.output.ctype.is_struct_union()
                and size not in (1, 2, 4, 8)):
            # A struct of 3, 5, 6 or 7 bytes arrives in one whole register,
            # but storing it into its home needs a move of its own size and
            # there is no 3-byte move. Widening is not an option here: slots
            # are packed, so writing eight bytes clobbers the neighbouring
            # local -- that produces a program that compiles and then
            # corrupts its own frame, which is far worse than not compiling.
            # Doing it properly means splitting the store into chunks, which
            # this does not yet do.
            #
            # Reporting beats the alternative: without this the backend
            # raises `unexpected register size` from inside register naming,
            # handing the user a Python traceback for a valid program.
            raise CompilerError(
                "passing a struct of %d bytes by value is not supported; "
                "its size is not a register width, so it cannot be moved in "
                "one piece -- pass a pointer instead" % size)
        if self.arg_reg is None and isinstance(dest, MemSpot):
            r = get_reg()
            asm_code.add(asm_cmds.Mov(r, src, size))
            asm_code.add(asm_cmds.Mov(dest, r, size))
        else:
            asm_code.add(asm_cmds.Mov(dest, src, size))


class LoadStructArg(_ValueCmd):
    """Loads a struct-valued function argument into its memory home.

    SysV AMD64: a struct in the INTEGER class arrives in one or two
    consecutive integer registers (`regs`); a struct in the MEMORY class
    (size > 16, or one that did not fit the remaining registers) arrives on
    the stack starting at 8-byte slot `stack_index`. Either way the struct is
    copied into the parameter's memory home.
    """

    def __init__(self, output, regs=None, stack_index=None):
        self.output = output
        self.regs = regs
        self.stack_index = stack_index

    def inputs(self):
        return []

    def outputs(self):
        return [self.output]

    def clobber(self):
        return list(self.regs) if self.regs else []

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"):
        home = spotmap[self.output]
        size = self.output.ctype.size
        if self.regs is not None:
            # Register-passed: store each incoming eightbyte register into the
            # matching 8-byte slot of the home.
            i = 0
            for reg in self.regs:
                remaining = size - 8 * i
                chunk = next(cs for cs in (8, 4, 2, 1) if remaining >= cs)
                asm_code.add(asm_cmds.Mov(home.shift(8 * i), reg, chunk))
                i = i + 1
        else:
            # Stack-passed: copy `size` bytes from the incoming stack slots.
            src = MemSpot(spots.RBP, 16 + 8 * self.stack_index)
            r = get_reg()
            self.move_data(home, src, size, r, asm_code)


class UnpackArgs(ILCommand):
    """Unpack bit-packed integer parameters into their parameter homes.

    Used by the -f-pack-args calling convention. The caller has packed several
    small integer arguments by bit-offset into one or more argument registers
    (`regs`, the concrete RDI/RSI/... in use). Each PackField in `plan` names a
    parameter (by its index into `outs`), the register it lives in, its low bit
    offset, and its byte size. We deposit each field's low `size` bytes into the
    matching parameter home.
    """

    def __init__(self, outs, regs, plan):
        # outs - list of parameter ILValues, in positional order
        # regs - list of source argument registers (RegSpot), low register first
        # plan - list of pack_args.PackField
        self.outs = outs
        self.regs = regs
        self.plan = plan

    def inputs(self):
        return []

    def outputs(self):
        return list(self.outs)

    def clobber(self):
        # The packed source registers are consumed here.
        return list(self.regs)

    # General-purpose registers, used to find a scratch that is not a parameter
    # home when a memory-homed parameter needs one.
    _GPRS = [spots.RAX, spots.RDI, spots.RSI, spots.RDX, spots.RCX,
             spots.R8, spots.R9, spots.R10, spots.R11]

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"):
        n = len(self.regs)
        out_reg_homes = {spotmap[o] for o in self.outs
                         if isinstance(spotmap[o], RegSpot)}
        mem_dest = any(not isinstance(spotmap[o], RegSpot) for o in self.outs)

        # Fast path (no stack traffic): copy each packed source register into a
        # scratch register that is not a parameter home, then extract each field
        # from the copy straight into its home. Because the scratch copies are
        # never parameter homes, writing a home (even one that aliased a source
        # register) cannot corrupt a field we still need. A memory-homed
        # parameter additionally needs one work register. This is feasible
        # whenever enough registers are free of parameter homes -- the common
        # case; only when parameters fill nearly the whole register file do we
        # fall back to stack staging below.
        free = [r for r in self._GPRS if r not in out_reg_homes]
        need = n + (1 if mem_dest else 0)
        if len(free) >= need:
            staged = {self.regs[i]: free[i] for i in range(n)}
            for src_reg, copy in staged.items():
                if copy != src_reg:
                    asm_code.add(asm_cmds.Mov(copy, src_reg, 8))
            work = free[n] if mem_dest else None
            for field in self.plan:
                src = staged[self.regs[field.reg_index]]
                dest = spotmap[self.outs[field.arg_index]]
                size = field.size
                if isinstance(dest, RegSpot):
                    asm_code.add(asm_cmds.Mov(dest, src, 8))
                    if field.bit_offset:
                        asm_code.add(asm_cmds.Raw(
                            "shr %s, %d" % (dest.asm_str(8), field.bit_offset)))
                else:
                    asm_code.add(asm_cmds.Mov(work, src, 8))
                    if field.bit_offset:
                        asm_code.add(asm_cmds.Raw(
                            "shr %s, %d" % (work.asm_str(8), field.bit_offset)))
                    asm_code.add(asm_cmds.Mov(dest, work, size))
            return

        # Fallback: at -O0 every general register can simultaneously be a packed
        # source and a parameter's home, so register staging cannot avoid
        # collisions. Save the packed source registers to the stack, which frees
        # every register, then extract each field straight into its home. A
        # register-homed parameter is written in place; a memory-homed one
        # borrows a scratch register -- and one is always free precisely then,
        # because a memory-homed parameter means not all GPRs are parameter
        # homes.
        for reg in self.regs:
            asm_code.add(asm_cmds.Push(reg, None, 8))
        # After pushing regs[0..n-1] in order, regs[i] sits at [rsp+8*(n-1-i)].
        scratch = next((r for r in self._GPRS if r not in out_reg_homes), None)

        for field in self.plan:
            off = 8 * (n - 1 - field.reg_index)
            src = MemSpot(spots.RSP, off)
            dest = spotmap[self.outs[field.arg_index]]
            size = field.size
            if isinstance(dest, RegSpot):
                # Load the whole eightbyte then shift the field down; the high
                # bits hold other packed fields but are ignored wherever this
                # parameter is later read at its own width.
                asm_code.add(asm_cmds.Mov(dest, src, 8))
                if field.bit_offset:
                    asm_code.add(asm_cmds.Raw(
                        "shr %s, %d" % (dest.asm_str(8), field.bit_offset)))
            else:
                asm_code.add(asm_cmds.Mov(scratch, src, 8))
                if field.bit_offset:
                    asm_code.add(asm_cmds.Raw(
                        "shr %s, %d" % (scratch.asm_str(8), field.bit_offset)))
                asm_code.add(asm_cmds.Mov(dest, scratch, size))

        asm_code.add(asm_cmds.AsmAdd(spots.RSP, LiteralSpot(str(8 * n)), 8))


class Set(_ValueCmd):
    """SET - sets output IL value to arg IL value.

    SET converts between all scalar types, so the output and arg IL values
    need not have the same type if both are scalar types. If either one is
    a struct type, the other must be the same struct type.

    TODO: split this up into finer IL commands.
    """
    def __init__(self, output, arg): # noqa D102
        self.output = output
        self.arg = arg

    def inputs(self): # noqa D102
        return [self.arg]

    def outputs(self): # noqa D102
        return [self.output]

    def rel_spot_pref(self): # noqa D102
        if self.output.ctype.weak_compat(ctypes.bool_t):
            return {}
        else:
            return {self.output: [self.arg]}

    def rel_spot_conf(self):
        if self.output.ctype.weak_compat(ctypes.bool_t):
            return {self.output: [self.arg]}
        else:
            return {}

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"): # noqa D102
        # SIMD bit-packing dispatch (opt-in). A write to a packed flag is
        # written through to memory + xmm15 everywhere; a read of a packed
        # flag inside a hot/interrupt routine is served from xmm15 (no load).
        if getattr(asm_code, "simd_pack_enabled", False):
            layout = asm_code.simd_pack
            out_slot = layout.slot_for_spot(spotmap[self.output])
            arg_slot = layout.slot_for_spot(spotmap[self.arg])
            if out_slot is not None:
                return self._set_packed_write(
                    out_slot, spotmap, get_reg, asm_code)
            if arg_slot is not None and asm_code.simd_pack_hot:
                return self._set_packed_read(
                    arg_slot, spotmap, get_reg, asm_code)

        if self.output.ctype.weak_compat(ctypes.bool_t):
            return self._set_bool(spotmap, get_reg, asm_code)

        elif (self.output.ctype.is_floating()
              or self.arg.ctype.is_floating()):
            return self._set_float(spotmap, get_reg, asm_code)

        elif isinstance(spotmap[self.arg], LiteralSpot):
            out_spot = spotmap[self.output]
            arg_spot = spotmap[self.arg]
            size = self.output.ctype.size
            # x86-64 can move only a sign-extended 32-bit immediate straight
            # to memory; a wider immediate (e.g. an immortal refcount) must go
            # through a register first. Test the literal value directly: routing
            # it through an `int()` local truncates it to 32 bits under the
            # self-host runtime (a 64-bit value like 10000000000 would wrap into
            # the 32-bit range and wrongly take the direct-to-memory path,
            # emitting an unencodable `mov mem, imm64`).
            val = arg_spot.value
            if (isinstance(out_spot, MemSpot) and size == 8
                    and isinstance(val, int)
                    and not (-2**31 <= val < 2**31)):
                r = get_reg()
                asm_code.add(asm_cmds.Mov(r, arg_spot, size))
                asm_code.add(asm_cmds.Mov(out_spot, r, size))
            else:
                asm_code.add(asm_cmds.Mov(out_spot, arg_spot, size))

        elif self.output.ctype.size <= self.arg.ctype.size:
            if spotmap[self.output] == spotmap[self.arg]:
                return

            if isinstance(spotmap[self.output], RegSpot):
                r = spotmap[self.output]
            elif isinstance(spotmap[self.arg], RegSpot):
                r = spotmap[self.arg]
            else:
                r = get_reg()

            self.move_data(spotmap[self.output], spotmap[self.arg],
                           self.output.ctype.size, r, asm_code)

        else:
            r = get_reg([spotmap[self.output], spotmap[self.arg]])

            # Move from arg_asm -> r_asm
            if self.arg.ctype.signed:
                asm_code.add(asm_cmds.Movsx(r, spotmap[self.arg],
                                            self.output.ctype.size,
                                            self.arg.ctype.size))
            elif self.arg.ctype.size == 4:
                asm_code.add(asm_cmds.Mov(r, spotmap[self.arg], 4))
            else:
                asm_code.add(asm_cmds.Movzx(r, spotmap[self.arg],
                                            self.output.ctype.size,
                                            self.arg.ctype.size))

            # If necessary, move from r_asm -> output_asm
            if r != spotmap[self.output]:
                asm_code.add(asm_cmds.Mov(spotmap[self.output],
                                          r, self.output.ctype.size))

    def _set_packed_write(self, slot, spotmap, get_reg, asm_code):
        """Emit a write-through store of self.arg into a packed flag slot."""
        # Materialize the source value into a scratch register at the correct
        # size (handles literal / register / memory sources uniformly).
        val_reg = get_reg()
        self.move_data(val_reg, spotmap[self.arg],
                       self.output.ctype.size, val_reg, asm_code)
        # Two more distinct scratch registers for the bit-insert sequence.
        acc_reg = get_reg(None, [val_reg])
        msk_reg = get_reg(None, [val_reg, acc_reg])
        asm_code.simd_pack.emit_write(asm_code, slot, val_reg, acc_reg, msk_reg)

    def _set_packed_read(self, slot, spotmap, get_reg, asm_code):
        """Emit a zero-latency (register-only) read of a packed flag slot."""
        out_spot = spotmap[self.output]
        if isinstance(out_spot, RegSpot):
            dst = out_spot
        else:
            dst = get_reg()
        asm_code.simd_pack.emit_read(asm_code, slot, dst)
        if dst != out_spot:
            asm_code.add(asm_cmds.Mov(out_spot, dst, self.output.ctype.size))

    def _set_float(self, spotmap, get_reg, asm_code):
        """SET where source and/or destination is floating-point."""
        from shivyc.spots import XMM0
        out_spot = spotmap[self.output]
        arg_spot = spotmap[self.arg]
        out_t, arg_t = self.output.ctype, self.arg.ctype

        def fmov(size):
            return asm_cmds.Movss if size == 4 else asm_cmds.Movsd

        if out_t.is_floating() and arg_t.is_floating():
            if arg_t.size == out_t.size:
                if out_spot == arg_spot:
                    return
                asm_code.add(fmov(arg_t.size)(XMM0, arg_spot, arg_t.size))
                asm_code.add(fmov(out_t.size)(out_spot, XMM0, out_t.size))
            else:
                asm_code.add(fmov(arg_t.size)(XMM0, arg_spot, arg_t.size))
                conv = (asm_cmds.Cvtsd2ss if arg_t.size == 8
                        else asm_cmds.Cvtss2sd)
                asm_code.add(conv(XMM0, XMM0, out_t.size))
                asm_code.add(fmov(out_t.size)(out_spot, XMM0, out_t.size))

        elif out_t.is_floating():
            src = arg_spot
            isize = arg_t.size if arg_t.size in (4, 8) else 8
            if isinstance(arg_spot, LiteralSpot) or arg_t.size not in (4, 8):
                r = get_reg()
                self.move_data(r, arg_spot, isize, r, asm_code)
                src = r
            conv = asm_cmds.Cvtsi2ss if out_t.size == 4 else asm_cmds.Cvtsi2sd
            asm_code.add(conv(XMM0, src, isize))
            asm_code.add(fmov(out_t.size)(out_spot, XMM0, out_t.size))

        else:
            asm_code.add(fmov(arg_t.size)(XMM0, arg_spot, arg_t.size))
            osize = out_t.size if out_t.size in (4, 8) else 4
            dst = out_spot if isinstance(out_spot, RegSpot) else get_reg()
            conv = (asm_cmds.Cvttss2si if arg_t.size == 4
                    else asm_cmds.Cvttsd2si)
            asm_code.add(conv(dst, XMM0, max(osize, 4)))
            if dst != out_spot:
                asm_code.add(asm_cmds.Mov(out_spot, dst, out_t.size))

    def _set_bool(self, spotmap, get_reg, asm_code):
        """Emit code for SET command if arg is boolean type."""
        # When any scalar value is converted to _Bool, the result is 0 if the
        # value compares equal to 0; otherwise, the result is 1

        # If arg_asm is a LITERAL or conflicts with output, move to register.
        if (isinstance(spotmap[self.arg], LiteralSpot)
              or spotmap[self.arg] == spotmap[self.output]):
            r = get_reg([], [spotmap[self.output]])
            asm_code.add(
                asm_cmds.Mov(r, spotmap[self.arg], self.arg.ctype.size))
            arg_spot = r
        else:
            arg_spot = spotmap[self.arg]

        global _set_bool_label_count
        _set_bool_label_count = _set_bool_label_count + 1
        label = "__shivyc_setbool_%d" % _set_bool_label_count
        output_spot = spotmap[self.output]

        zero = LiteralSpot("0")
        one = LiteralSpot("1")

        asm_code.add(asm_cmds.Mov(output_spot, zero, self.output.ctype.size))
        asm_code.add(asm_cmds.Cmp(arg_spot, zero, self.arg.ctype.size))
        asm_code.add(asm_cmds.Je(label))
        asm_code.add(asm_cmds.Mov(output_spot, one, self.output.ctype.size))
        asm_code.add(asm_cmds.AsmLabel(label))


class AddrOf(ILCommand):
    """Gets address of given variable.

    `output` must have type pointer to the type of `var`.

    """
    output: "ILValue"

    def __init__(self, output, var):  # noqa D102
        self.output = output
        self.var = var

    def inputs(self):  # noqa D102
        return [self.var]

    def outputs(self):  # noqa D102
        return [self.output]

    def references(self):  # noqa D102
        return {self.output: [self.var]}

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"):  # noqa D102
        r = get_reg([spotmap[self.output]])
        asm_code.add(asm_cmds.Lea(r, home_spots[self.var]))

        if r != spotmap[self.output]:
            size = self.output.ctype.size
            asm_code.add(asm_cmds.Mov(spotmap[self.output], r, size))


class ReadAt(_ValueCmd):
    """Reads value at given address.

    `addr` must have type pointer to the type of `output`

    """

    addr: "ILValue"

    def __init__(self, output, addr: "ILValue"):  # noqa D102
        self.output = output
        self.addr = addr

    def inputs(self):  # noqa D102
        return [self.addr]

    def outputs(self):  # noqa D102
        return [self.output]

    def indir_read(self):  # noqa D102
        return [self.addr]

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"):  # noqa D102
        addr_spot = spotmap[self.addr]
        output_spot = spotmap[self.output]

        if isinstance(addr_spot, RegSpot):
            addr_r = addr_spot
        else:
            addr_r = get_reg([], [output_spot])
            # Load the address operand at the pointer's own width. Under
            # -f-pointer-compression a pointer is 4 bytes: a 32-bit load
            # zero-extends into the full 64-bit register, giving the real
            # low-4GiB address. A hardcoded 8 would read 4 bytes past the
            # pointer and corrupt the high half of the address.
            asm_code.add(asm_cmds.Mov(addr_r, addr_spot, self.addr.ctype.size))

        indir_spot = MemSpot(addr_r)
        if isinstance(output_spot, RegSpot):
            temp_reg = output_spot
        else:
            temp_reg = get_reg([], [addr_r])

        self.move_data(output_spot, indir_spot, self.output.ctype.size,
                       temp_reg, asm_code)


class SetAt(_ValueCmd):
    """Sets value at given address.

    `addr` must have type pointer to the type of `val`

    """

    addr: "ILValue"

    def __init__(self, addr: "ILValue", val):  # noqa D102
        self.addr = addr
        self.val = val

    def inputs(self):  # noqa D102
        return [self.addr, self.val]

    def outputs(self):  # noqa D102
        return []

    def indir_write(self):  # noqa D102
        return [self.addr]

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"):  # noqa D102
        addr_spot = spotmap[self.addr]
        value_spot = spotmap[self.val]

        if isinstance(addr_spot, RegSpot):
            addr_r = addr_spot
        else:
            addr_r = get_reg([], [value_spot])
            # See ReadAt: load the destination address at the pointer's own
            # width so a compressed (4-byte) pointer zero-extends correctly.
            asm_code.add(asm_cmds.Mov(addr_r, addr_spot, self.addr.ctype.size))

        indir_spot = MemSpot(addr_r)
        if isinstance(value_spot, RegSpot):
            temp_reg = value_spot
        else:
            temp_reg = get_reg([], [addr_r])

        self.move_data(indir_spot, value_spot, self.val.ctype.size,
                       temp_reg, asm_code)


class _RelCommand(_ValueCmd):
    """Parent class for the relative commands."""

    def __init__(self, val, base, chunk, count: "object"):  # noqa D102
        self.val = val
        self.base = base
        self.chunk = chunk
        self.count = count

        # Keep track of which registers have been used from a call to
        # get_reg so we don't accidentally reuse them.
        self._used_regs = []

    def get_rel_spot(self, spotmap, get_reg, asm_code):
        """Get a relative spot for the relative value."""

        # If there's no count, we only need to shift by the chunk
        if not self.count:
            return spotmap[self.base].shift(self.chunk)

        # If there is a count in a literal spot, we're good to go. Also,
        # if count is already in a register, we're good to go by just using
        # that register for the count. (Because we require the count be 32-
        # or 64-bit, we know the full register stores exactly the value of
        # count).
        if (isinstance(spotmap[self.count], LiteralSpot)
             or isinstance(spotmap[self.count], RegSpot)):
            return spotmap[self.base].shift(self.chunk, spotmap[self.count])

        # Otherwise, move count to a register.
        r = get_reg([], [spotmap[self.val]] + self._used_regs)
        self._used_regs.append(r)

        count_size = self.count.ctype.size
        asm_code.add(asm_cmds.Mov(r, spotmap[self.count], count_size))

        return spotmap[self.base].shift(self.chunk, r)

    def get_reg_spot(self, reg_val, spotmap, get_reg):
        """Get a register or literal spot for self.reg_val."""

        spot = spotmap[reg_val]
        # A literal that fits in a sign-extended 32-bit immediate can be used
        # directly (it stores fine to memory). A wider literal needs a real
        # scratch register, because `mov mem, imm64` is not encodable and the
        # move must go through `mov reg, imm64` (movabs).
        if isinstance(spot, LiteralSpot):
            # A symbolic literal (e.g. an enum constant) has a string value,
            # so its magnitude is not known here; fall through and give it a
            # scratch register, which is always valid.
            if isinstance(spot.value, int) and \
                    -(2 ** 31) <= spot.value < 2 ** 31:
                return spot
        elif isinstance(spot, RegSpot):
            return spot

        val_spot = get_reg([], ([spotmap[self.count]] if self.count else [])
                           + self._used_regs)
        self._used_regs.append(val_spot)
        return val_spot


class SetRel(_RelCommand):
    """Sets value relative to given object.

    val - ILValue representing the value to set at given location.

    base - ILValue representing the base object. Note this is the base
    object itself, not the address of the base object.

    chunk - A Python integer representing the size of each chunk of offset
    (see below for a more clear explanation)

    count - If provided, a 64-bit integral ILValue representing the
    number of chunks of offset. If this value is provided, then `chunk`
    must be in {1, 2, 4, 8}.

    In summary, if `count` is provided, then the address of the object
    represented by this LValue is:

        &base + chunk * count

    and if `count` is not provided, the address is just

        &base + chunk
    """

    def __init__(self, val, base, chunk=0, count=None):  # noqa D102
        super().__init__(val, base, chunk, count)
        self.val = val

    def inputs(self):  # noqa D102
        if self.count:
            return [self.val, self.base, self.count]
        else:
            return [self.base, self.val]

    def outputs(self):  # noqa D102
        return []

    def references(self):  # noqa D102
        return {None: [self.base]}

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"):  # noqa D102
        if not isinstance(spotmap[self.base], MemSpot):
            raise NotImplementedError("expected base in memory spot")

        rel_spot = self.get_rel_spot(spotmap, get_reg, asm_code)
        val_size = self.val.ctype.size

        # A floating value lives in memory and moves through the xmm scratch
        # register; the integer get_reg/move_data path does not apply.
        if self.val.ctype.is_floating():
            from shivyc.spots import XMM0
            fmov = asm_cmds.Movss if val_size == 4 else asm_cmds.Movsd
            asm_code.add(fmov(XMM0, spotmap[self.val], val_size))
            asm_code.add(fmov(rel_spot, XMM0, val_size))
            return

        reg = self.get_reg_spot(self.val, spotmap, get_reg)
        self.move_data(rel_spot, spotmap[self.val], val_size, reg, asm_code)


class AddrRel(_RelCommand):
    """Gets the address of a location relative to a given object.

    For further documentation, see SetRel.

    """
    def __init__(self, output, base, chunk=0, count=None):  # noqa D102
        super().__init__(output, base, chunk, count)
        self.output = output

    def inputs(self):  # noqa D102
        return [self.base, self.count] if self.count else [self.base]

    def outputs(self):  # noqa D102
        return [self.output]

    def references(self):  # noqa D102
        return {self.output: [self.base]}

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"):  # noqa D102
        if not isinstance(spotmap[self.base], MemSpot):
            raise NotImplementedError("expected base in memory spot")

        rel_spot = self.get_rel_spot(spotmap, get_reg, asm_code)
        out_spot = self.get_reg_spot(self.output, spotmap, get_reg)
        asm_code.add(asm_cmds.Lea(out_spot, rel_spot))

        if out_spot != spotmap[self.output]:
            asm_code.add(asm_cmds.Mov(spotmap[self.output], out_spot, 8))


class ReadRel(_RelCommand):
    """Reads the value at a location relative to a given object.

    For further documentation, see SetRel.

    """

    def __init__(self, output, base, chunk=0, count=None):  # noqa D102
        super().__init__(output, base, chunk, count)
        self.output = output

    def inputs(self):  # noqa D102
        return [self.base, self.count] if self.count else [self.base]

    def outputs(self):  # noqa D102
        return [self.output]

    def references(self):  # noqa D102
        return {None: [self.base]}

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"):  # noqa D102
        if not isinstance(spotmap[self.base], MemSpot):
            raise NotImplementedError("expected base in memory spot")

        rel_spot = self.get_rel_spot(spotmap, get_reg, asm_code)
        out_size = self.output.ctype.size

        # Floating destination: move through the xmm scratch into its memory
        # home rather than through a GPR.
        if self.output.ctype.is_floating():
            from shivyc.spots import XMM0
            fmov = asm_cmds.Movss if out_size == 4 else asm_cmds.Movsd
            asm_code.add(fmov(XMM0, rel_spot, out_size))
            asm_code.add(fmov(spotmap[self.output], XMM0, out_size))
            return

        reg = self.get_reg_spot(self.output, spotmap, get_reg)
        self.move_data(spotmap[self.output], rel_spot, out_size, reg, asm_code)


class VaStartAddr(ILCommand):
    """Compute the address of the first variadic argument.

    Variadic functions receive all arguments on the stack, so the first
    variadic argument lives at [rbp + 16 + 8*named_count], where named_count
    is the number of named parameters.
    """
    output: "ILValue"

    def __init__(self, output, named_count):
        self.output = output
        self.named_count = named_count

    def inputs(self):
        return []

    def outputs(self):
        return [self.output]

    def clobber(self):
        return []

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"):
        off = 16 + 8 * self.named_count
        src = MemSpot(spots.RBP, off)
        dest = spotmap[self.output]
        if isinstance(dest, RegSpot):
            asm_code.add(asm_cmds.Lea(dest, src))
        else:
            r = get_reg()
            asm_code.add(asm_cmds.Lea(r, src))
            asm_code.add(asm_cmds.Mov(dest, r, 8))
