"""IL command for a narrow, Minikraft-driven subset of GCC inline assembly.

This is deliberately NOT a general extended-asm implementation. It supports
exactly the vocabulary the Minikraft unikernel uses:

* bare side-effect-only templates (``mfence``, ``hlt``, ``sti``, the empty
  compiler barrier, ``lock; addl ...``), and
* operand templates with the constraints ``a``/``=a`` (accumulator, sized to
  the operand), ``Nd``/``d`` (treated as ``rdx``), and ``m`` (a memory operand,
  realized as the address held in ``rcx``).

Operands are numbered output-first to match GCC. Templates are emitted verbatim
inside an ``.att_syntax``/``.intel_syntax`` toggle, since Minikraft's templates
are written in AT&T syntax while ShivyC's body is Intel syntax. The ``memory``
and ``cc`` clobbers are no-ops here because ShivyC does not reorder across the
command.
"""

import shivyc.asm_cmds as asm_cmds
import shivyc.spots as spots
from shivyc.il_cmds.base import ILCommand


def _att_reg(reg64, size):
    """AT&T name (with % prefix) of a register by operand size in bytes."""
    idx = {1: 3, 2: 2, 4: 1, 8: 0}.get(size, 0)
    return "%" + spots.RegSpot.reg_map[reg64][idx]




def _resolve_asm_dialect(template):
    """Resolve GCC inline-asm dialect alternatives to the AT&T variant.

    GCC templates may use ``{att-variant|intel-variant|...}`` to pick text by
    assembler dialect. ShivyCX emits inline asm under ``.att_syntax``, so the
    first alternative is always selected. A lone ``{`` with no matching
    ``|``/``}`` (as in CPython's ``"{movq %%rsp, %0"``) is treated as a stray
    dialect marker and dropped.
    """
    out = []
    i, n = 0, len(template)
    while i < n:
        ch = template[i]
        if ch == "{":
            j = i + 1
            while j < n and template[j] not in "|}":
                out.append(template[j])
                j += 1
            # Skip any remaining alternatives up to and including the close.
            while j < n and template[j] != "}":
                j += 1
            i = j + 1 if (j < n and template[j] == "}") else j
        elif ch == "}":
            i += 1  # stray close brace
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class InlineAsm(ILCommand):
    """A single inline-asm statement.

    outputs / inputs are lists of (constraint_str, ILValue). For ``m`` inputs
    the ILValue holds the operand's address.
    """

    def __init__(self, template, outputs, inputs, clobbers):
        self.template = template
        self.out_ops = outputs
        self.in_ops = inputs
        self.clobbers = clobbers

    _NAME_TO_SPOT = {
        "rax": spots.RAX, "rcx": spots.RCX, "rdx": spots.RDX,
        "rsi": spots.RSI, "rdi": spots.RDI, "r8": spots.R8,
        "r9": spots.R9, "r10": spots.R10, "r11": spots.R11,
        "rbx": spots.RegSpot("rbx"),
    }

    def inputs(self):
        # Values read by the asm: explicit inputs, plus the address of every
        # memory output (`=m`), which is consumed to locate the store.
        ins = [v for _, v in self.in_ops]
        ins += [v for c, v in self.out_ops if "m" in c]
        # A read-write register operand (`+r`) is read as well as written, so
        # it must be listed here or the allocator treats it as dead on entry
        # and drops the move that gave it its starting value.
        ins += [v for c, v in self.out_ops if "m" not in c and "+" in c]
        return ins

    def outputs(self):
        # Values defined by the asm: only register outputs. A memory output's
        # ILValue is its address (an input), not a result the asm produces.
        return [v for c, v in self.out_ops if "m" not in c]

    # Pool of general registers used to satisfy `r`/`=r` operands and to stage
    # the address of memory (`m`/`=m`) operands. Ordered so the common fixed
    # registers come first; rbp/rsp are excluded, and rbx is omitted to avoid
    # having to preserve a callee-saved register (no supported asm needs more
    # than these nine operand registers).
    _POOL = ["rax", "rdi", "rsi", "rdx", "rcx", "r8", "r9", "r10", "r11"]

    _LETTER = {"a": "rax", "b": "rbx", "c": "rcx", "d": "rdx",
               "S": "rsi", "D": "rdi"}

    def _assign(self):
        """Deterministically assign each operand a location.

        Returns (ops, slots) where ops is a list of (io, constraint, ILValue)
        with io in {"out", "in"} (outputs first, GCC numbering), and slots is a
        parallel list of ("reg", name) or ("mem", name) -- "mem" meaning the
        register holds the operand's address and the operand is `(%name)`.

        This is pure (no spotmap/get_reg), so clobber() and make_asm() agree on
        the registers used.
        """
        ops = ([("out", c, v) for c, v in self.out_ops]
               + [("in", c, v) for c, v in self.in_ops])
        slots = [None] * len(ops)
        used = set()

        def claim(name):
            used.add(name)
            return name

        def next_free():
            for name in self._POOL:
                if name not in used:
                    return claim(name)
            raise NotImplementedError(
                "inline asm needs more operand registers than supported")

        # Pass 1: fixed-register letters (a/b/c/d/S/D) and `register __asm__`
        # bindings on `r` operands.
        for i, (io, c, v) in enumerate(ops):
            if "m" in c:
                continue
            rn = None
            for ltr, name in self._LETTER.items():
                if ltr in c:
                    rn = name
                    break
            if rn is None and "r" in c:
                rn = getattr(v, "asm_reg", None)
            if rn is not None:
                slots[i] = ("reg", rn)
                used.add(rn)

        # Pass 2: plain `r`/`=r` operands without a matching digit get a fresh
        # register.
        for i, (io, c, v) in enumerate(ops):
            if slots[i] is not None or "m" in c:
                continue
            if any(ch.isdigit() for ch in c):
                continue  # matching constraint, resolved in pass 3
            if "r" in c:
                slots[i] = ("reg", next_free())
            else:
                raise NotImplementedError(
                    f"unsupported inline-asm constraint {c!r}")

        # Pass 3: matching constraints ("0", "1", ...) share the referenced
        # operand's location.
        for i, (io, c, v) in enumerate(ops):
            if slots[i] is not None:
                continue
            digit = next((ch for ch in c if ch.isdigit()), None)
            if digit is not None and slots[int(digit)] is not None:
                slots[i] = slots[int(digit)]

        # Pass 4: memory operands get a register to hold their address.
        for i, (io, c, v) in enumerate(ops):
            if slots[i] is not None:
                continue
            if "m" in c:
                slots[i] = ("mem", next_free())
            else:
                raise NotImplementedError(
                    f"unsupported inline-asm constraint {c!r}")
        return ops, slots

    def abs_spot_pref(self):
        # Ask the allocator to place each operand directly in the register the
        # asm needs it in. This lets the allocator insert any required moves
        # (in correct, hazard-free order) before the command, so the moves in
        # make_asm become no-ops -- avoiding a parallel-move clobber such as a
        # memory operand's address-staging register colliding with an input.
        name_to_spot = self._NAME_TO_SPOT
        ops, slots = self._assign()
        prefs = {}
        for (io, c, v), (kind, name) in zip(ops, slots):
            if name in name_to_spot:
                prefs.setdefault(v, [name_to_spot[name]])
        return prefs

    def clobber(self):
        # Report every register the asm writes (operand and address-staging
        # registers, plus the explicit clobber list). This keeps the allocator
        # from holding live values there, which also guarantees no operand's
        # source spot is one of these registers -- so the operand moves below
        # cannot clobber each other.
        name_to_spot = self._NAME_TO_SPOT
        _, slots = self._assign()
        regs = set()
        for kind, name in slots:
            if name in name_to_spot:
                regs.add(name_to_spot[name])
        for cl in self.clobbers:
            cl = cl.strip().strip('"')
            if cl in name_to_spot:
                regs.add(name_to_spot[cl])
        return list(regs)

    def _emit_parallel(self, moves, asm_code):
        """Emit register/address loads (dest_reg, src_spot, size) in an order
        where no move overwrites a register another move still needs to read,
        breaking any cycle with a scratch register."""
        pending = [(d, s, sz) for (d, s, sz) in moves if d != s]
        # Deduplicate identical moves (a matching operand reuses its source).
        seen, uniq = set(), []
        for m in pending:
            # Spot.__str__ is not reliably a string (LiteralSpot returns its
            # int value); asm_str always yields a string for a stable key.
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
                raise NotImplementedError("inline asm: move scheduling failed")
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
            # A cycle remains (each dest is another move's source). Relocate one
            # source into a free scratch register, then redirect reads to it.
            used = {str(d) for d, _, _ in pending}
            used |= {str(s) for _, s, _ in pending if is_reg(s)}
            temp = None
            for nm in self._POOL:
                if str(self._NAME_TO_SPOT[nm]) not in used:
                    temp = self._NAME_TO_SPOT[nm]
                    break
            if temp is None:
                raise NotImplementedError(
                    "inline asm: no scratch register to break a move cycle")
            src0 = pending[0][1]
            asm_code.add(asm_cmds.Mov(temp, src0, 8))
            pending = [(d, (temp if s == src0 else s), sz)
                       for (d, s, sz) in pending]

    def make_asm(self, spotmap, home_spots, get_reg, asm_code: "asm_gen.ASMCode"):
        name_to_spot = self._NAME_TO_SPOT
        ops, slots = self._assign()
        operand_strs = []
        pre_moves = []   # (dest_reg_spot, src_spot, size) before the asm
        post_moves = []  # (dest_spot, src_reg, size) after the asm

        for i, (io, c, v) in enumerate(ops):
            kind, name = slots[i]
            spot = name_to_spot[name]
            if kind == "mem":
                # The ILValue holds the operand's address; stage it and refer
                # to the operand as a memory reference. (Memory operands carry
                # no register-size; the instruction's suffix sizes the access.)
                pre_moves.append((spot, spotmap[v], 8))
                operand_strs.append("(%" + name + ")")
                continue
            size = v.ctype.size
            operand_strs.append(_att_reg(name, size))
            if io == "in":
                # Load the input value into its register before the asm.
                pre_moves.append((spot, spotmap[v], size))
            else:
                # Register output: copy the result back to its lvalue after.
                # A read-write operand (`+r`) also has to be *loaded* first --
                # it is an input too. Without the pre-move the asm operated on
                # whatever the allocator left in the register, so
                # `addl $37, %0` on a variable holding 5 produced an address.
                if "+" in c:
                    pre_moves.append((spot, spotmap[v], size))
                post_moves.append((spotmap[v], spot, size))

        self._emit_parallel(pre_moves, asm_code)

        text = _resolve_asm_dialect(self.template).replace("%%", "%")
        # Substitute %N high-to-low so %1 does not partially match %10.
        for i in reversed(range(len(operand_strs))):
            text = text.replace("%" + str(i), operand_strs[i])

        if text.strip():
            asm_code.add(asm_cmds.Raw(".att_syntax prefix"))
            for line in text.split("\n"):
                if line.strip():
                    asm_code.add(asm_cmds.Raw(line.strip()))
            asm_code.add(asm_cmds.Raw(".intel_syntax noprefix"))

        for dest_spot, src_reg, size in post_moves:
            if dest_spot != src_reg:
                asm_code.add(asm_cmds.Mov(dest_spot, src_reg, size))
