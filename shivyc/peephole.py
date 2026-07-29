"""IL-level peephole optimizations, run per function just before register
allocation and code generation.

These are basic, target-independent simplifications that the -O0 code generator
otherwise leaves on the table, narrowing the gap with an optimizing compiler:

  * compare-and-branch fusion -- a comparison that feeds directly into a
    conditional jump becomes a single `cmp; jcc`, instead of materializing a
    0/1 boolean and then testing it (saves ~4 instructions per loop/if test).
  * arithmetic identities -- `x * 1`, `x + 0`, `x - 0` become a plain copy and
    `x * 0` becomes a load of 0 (these arise from address arithmetic, macro
    expansion, and generic code).

Each pass takes and returns the command list for a single function.
"""

import shivyc.il_cmds.compare as compare_cmds
import shivyc.il_cmds.control as control_cmds
import shivyc.il_cmds.math as math_cmds
import shivyc.il_cmds.value as value_cmds
import shivyc.ctypes as ctypes
from shivyc.il_gen import ILValue


def _input_use_counts(commands):
    counts = {}
    for c in commands:
        for v in c.inputs():
            counts[v] = counts.get(v, 0) + 1
    return counts


def fuse_compare_jumps(commands, il_code):
    """Fuse `cmp -> conditional jump` into a single compare-and-branch.

    Only fires when the comparison's boolean result is consumed solely by the
    immediately following JumpZero/JumpNotZero, both operands are integers, and
    the first operand is not a literal. (A literal first operand makes the
    comparison codegen swap the operands, which would invert an ordering test;
    refusing to fuse there keeps the branch direction correct.)
    """
    n = len(commands)
    if n < 2:
        return commands
    uses = _input_use_counts(commands)
    out = []
    i = 0
    while i < n:
        c = commands[i]
        if isinstance(c, compare_cmds._GeneralCmp) and i + 1 < n \
                and getattr(c, "fuse", None) is None:
            nxt = commands[i + 1]
            if isinstance(nxt, (control_cmds.JumpZero, control_cmds.JumpNotZero)) \
                    and nxt.cond is c.output \
                    and uses.get(c.output, 0) == 1 \
                    and c.arg1 not in il_code.literals \
                    and not c.arg1.ctype.is_floating() \
                    and not c.arg2.ctype.is_floating():
                # JumpZero branches when the comparison is false -> negate.
                c.fuse = (nxt.label, isinstance(nxt, control_cmds.JumpZero))
                out.append(c)
                i += 2
                continue
        out.append(c)
        i += 1
    return out


def _lit(il_code, v):
    """The integer value of a literal IL value (stored as a string), or None."""
    s = il_code.literals.get(v)
    if s is None:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _pow2_shift(n):
    """`k` if `n` is 2**k with k >= 1, else None."""
    if n is None or n < 2 or (n & (n - 1)) != 0:
        return None
    return n.bit_length() - 1


def strength_reduce_divmod(commands, il_code):
    """Turn unsigned division and modulo by a power of two into shift and mask.

    `idiv` costs tens of cycles where `shr` and `and` cost one, and kernel code
    divides by powers of two constantly -- page sizes, bitmap words, alignment.
    On a benchmark built from Redox's own abstractions this single rewrite was
    worth 6x (0.114s to 0.019s), far more than anything else measured
    including inlining, which was worth nothing at all.

    **Unsigned only.** For a signed operand `x / 2^k` is not `x >> k` and
    `x % 2^k` is not `x & (2^k - 1)`: C rounds division toward zero while an
    arithmetic shift rounds toward negative infinity, so -1 / 2 is 0 but
    -1 >> 1 is -1. Getting that wrong is a silent wrong answer, so a signed
    operand is left alone rather than given the correction sequence, which is
    several instructions and worth much less.
    """
    out = []
    for c in commands:
        repl = None
        if isinstance(c, (math_cmds.Div, math_cmds.Mod)):
            ctype = c.arg1.ctype
            unsigned = (not ctype.is_floating()
                        and not getattr(ctype, "signed", True)
                        and not getattr(c.arg2.ctype, "signed", True))
            k = _pow2_shift(_lit(il_code, c.arg2))
            if unsigned and k is not None:
                if isinstance(c, math_cmds.Div):
                    sh = ILValue(c.arg2.ctype)
                    il_code.register_literal_var(sh, str(k))
                    repl = math_cmds.RBitShift(c.output, c.arg1, sh)
                else:
                    mask = ILValue(c.arg2.ctype)
                    il_code.register_literal_var(mask, str((1 << k) - 1))
                    repl = math_cmds.BitAnd(c.output, c.arg1, mask)
        out.append(repl if repl is not None else c)
    return out


def simplify_arith(commands, il_code):
    """Replace arithmetic identities with copies / constant loads."""
    out = []
    for c in commands:
        repl = None
        if isinstance(c, math_cmds.Mult):
            a, b = _lit(il_code, c.arg1), _lit(il_code, c.arg2)
            if b == 1:
                repl = value_cmds.Set(c.output, c.arg1)
            elif a == 1:
                repl = value_cmds.Set(c.output, c.arg2)
        elif isinstance(c, math_cmds.Add):
            a, b = _lit(il_code, c.arg1), _lit(il_code, c.arg2)
            if b == 0:
                repl = value_cmds.Set(c.output, c.arg1)
            elif a == 0:
                repl = value_cmds.Set(c.output, c.arg2)
        elif isinstance(c, math_cmds.Subtr):
            if _lit(il_code, c.arg2) == 0:
                repl = value_cmds.Set(c.output, c.arg1)
        out.append(repl if repl is not None else c)
    return out


# Pure, non-trapping IL command types: hoisting them out of a loop cannot
# introduce a fault (no division, no pointer dereference) or a side effect (no
# store, no call). Set copies an allocated value; arithmetic/bitwise/address
# computations are pure register ops.
def _hoistable_types():
    return (
        value_cmds.Set,
        value_cmds.AddrOf,
        math_cmds.Add, math_cmds.Subtr, math_cmds.Mult,
        math_cmds.BitAnd, math_cmds.BitOr, math_cmds.BitXor,
        math_cmds.Neg, math_cmds.Not,
        math_cmds.RBitShift, math_cmds.LBitShift,
    )


# Commands that write memory or call out: their presence makes a loop "unclean"
# because a store or call could change the memory a hoisted Set reads. Plain
# loads (ReadAt/ReadRel) do not modify memory, so they are allowed in the loop;
# they simply are not themselves hoisted (not in the hoistable set), which
# avoids any fault/aliasing concern from moving a dereference.
def _has_side_effects(c):
    return isinstance(c, (
        control_cmds.Call,
        value_cmds.SetAt, value_cmds.SetRel,
    ))


def hoist_loop_invariants(commands, il_code):
    """Hoist loop-invariant pure computations into a loop preheader.

    Conservative: only fires on a structured loop whose body contains no calls,
    stores, or loads (so all memory is provably invariant), and only hoists
    pure non-trapping commands whose operands are all defined outside the loop.
    This removes, e.g., a loop-invariant global reload from a tight numeric loop
    without risking aliasing or fault-introduction.
    """
    hoistable = _hoistable_types()
    changed = True
    guard = 0
    while changed and guard < 20:
        changed = False
        guard += 1
        labels = {c.label_name(): i for i, c in enumerate(commands)
                  if c.label_name()}
        # Count how many commands jump to each label (to require a single,
        # back-edge entry so the preheader is the only non-loop predecessor).
        target_counts = {}
        for c in commands:
            for t in c.targets():
                target_counts[t] = target_counts.get(t, 0) + 1

        for back_idx, c in enumerate(commands):
            tgts = c.targets()
            back_labels = [t for t in tgts if labels.get(t, back_idx) < back_idx]
            if not back_labels:
                continue
            start_idx = labels[back_labels[0]]
            body = commands[start_idx:back_idx + 1]

            # The hoisted commands are inserted immediately before start_idx, so
            # they only run if control falls through into that slot from the
            # preceding instruction. If that instruction is an unconditional
            # jump or a return, the preheader slot is unreachable -- this is the
            # case for a switch dispatch, whose backward case-match branch
            # (JumpZero -> case label) looks like a loop back-edge but whose
            # header label is preceded by the initial `jmp dispatch`. Hoisting
            # there would drop the computation entirely (a miscompile), so
            # require fall-through reachability into the preheader.
            if start_idx == 0 or isinstance(
                    commands[start_idx - 1],
                    (control_cmds.Jump, control_cmds.Return)):
                continue

            # Require a clean loop and a single jump-entry (the back-edge).
            if any(_has_side_effects(b) for b in body):
                continue
            if target_counts.get(back_labels[0], 0) != 1:
                continue
            # Don't touch loops with nested back-edges (keep it simple/safe).
            nested = False
            for j in range(start_idx, back_idx):
                for t in commands[j].targets():
                    ti = labels.get(t)
                    if ti is not None and ti < j and not (j == back_idx):
                        nested = True
            if nested:
                continue

            # Hoisting is only sound when each hoisted command executes on every
            # iteration -- i.e. dominates the back-edge. That is guaranteed only
            # if the loop body is a single basic block. If the body contains
            # internal control flow (a forward branch or an inner label between
            # the header and the back-edge), a "pure invariant" can sit on a
            # conditional path; moving it to the preheader runs it
            # unconditionally, and if its result is live after the loop (e.g. a
            # flag assigned only on the taken branch) the program's result
            # changes. Require a single-block body (the back-edge jump itself,
            # at back_idx, is excluded from this scan).
            internal_cf = False
            for j in range(start_idx + 1, back_idx):
                b = commands[j]
                if b.label_name() or b.targets():
                    internal_cf = True
                    break
            if internal_cf:
                continue

            defined_in_loop = set()
            def_count = {}
            for b in body:
                for o in b.outputs():
                    defined_in_loop.add(o)
                    def_count[o] = def_count.get(o, 0) + 1

            to_hoist = []
            for j in range(start_idx + 1, back_idx):  # skip start label, back-jump
                b = commands[j]
                if not isinstance(b, hoistable):
                    continue
                outs = b.outputs()
                ins = b.inputs()
                if len(outs) != 1:
                    continue
                out = outs[0]
                if def_count.get(out, 0) != 1:   # multiple defs -> not safe
                    continue
                if out in ins:                   # self-referential
                    continue
                if any(v in defined_in_loop for v in ins):
                    continue                     # an operand varies in the loop
                to_hoist.append(j)

            if not to_hoist:
                continue

            hoisted = [commands[j] for j in to_hoist]
            hoist_set = set(to_hoist)
            new_commands = []
            for i, cc in enumerate(commands):
                if i == start_idx:
                    new_commands.extend(hoisted)   # preheader (before the label)
                if i in hoist_set:
                    continue
                new_commands.append(cc)
            commands = new_commands
            changed = True
            break  # restart scan with updated indices

    return commands


# --- Induction-variable strength reduction --------------------------------
#
# In a loop, an address recomputed from the loop counter every iteration
# (`addr = base + scale*i + off`) is replaced by a pointer computed once in a
# preheader and advanced by a constant stride each iteration -- turning the
# index recompute + sign-extend + add-base sequence into a single `add`. This
# matches what gcc does for array traversals. Like gcc -O2, this assumes the
# affine index does not signed-overflow (so sign-extension is linear).

def _single_block_loop_range(commands, start_idx, back_idx, labels):
    """True if (start_idx, back_idx) is a straight-line loop body: no internal
    labels and no internal jumps except early exits (targets outside the loop)
    and the single back-edge at back_idx."""
    for j in range(start_idx + 1, back_idx + 1):
        c = commands[j]
        if c.label_name() is not None:
            return False
        if j == back_idx:
            continue
        for t in c.targets():
            ti = labels.get(t)
            if ti is not None and start_idx <= ti <= back_idx:
                return False  # internal jump back into the loop body
    return True


def _trace_stride(addr: "ILValue", def_of, commands, basic_ivs, defined, il_code,
                  chain, depth=0):
    """Trace `addr` back toward a single basic IV, accumulating the scale.

    Returns (iv, scale): addr changes by `scale` per unit of `iv`. scale 0 means
    `addr` is loop-invariant. Returns None if `addr` is not an affine function
    of at most one basic IV using only loop-invariant coefficients. Records the
    indices (into `commands`) of visited defining commands in `chain`.
    """
    if depth > 12:
        return None
    if addr in basic_ivs:
        return (addr, 1)
    if addr not in defined:
        return (addr, 0)
    if addr not in def_of:
        return None
    idx = def_of[addr]
    cmd = commands[idx]
    chain.append(idx)

    if isinstance(cmd, value_cmds.Set):
        return _trace_stride(cmd.arg, def_of, commands, basic_ivs, defined,
                             il_code, chain, depth + 1)
    if isinstance(cmd, (math_cmds.Add, math_cmds.Subtr)):
        a = _trace_stride(cmd.arg1, def_of, commands, basic_ivs, defined,
                          il_code, chain, depth + 1)
        b = _trace_stride(cmd.arg2, def_of, commands, basic_ivs, defined,
                          il_code, chain, depth + 1)
        if a is None or b is None:
            return None
        iv_a, s_a = a
        iv_b, s_b = b
        if s_a != 0 and s_b != 0:
            return None  # two varying terms -> not single-IV affine
        if isinstance(cmd, math_cmds.Subtr):
            s_b = -s_b
        if s_a != 0:
            return (iv_a, s_a)
        if s_b != 0:
            return (iv_b, s_b)
        return (iv_a, 0)
    if isinstance(cmd, math_cmds.Mult):
        la = _lit(il_code, cmd.arg1)
        lb = _lit(il_code, cmd.arg2)
        if lb is not None:
            inner = _trace_stride(cmd.arg1, def_of, commands, basic_ivs,
                                  defined, il_code, chain, depth + 1)
            if inner is None or inner[1] == 0:
                return None
            return (inner[0], inner[1] * lb)
        if la is not None:
            inner = _trace_stride(cmd.arg2, def_of, commands, basic_ivs,
                                  defined, il_code, chain, depth + 1)
            if inner is None or inner[1] == 0:
                return None
            return (inner[0], inner[1] * la)
        return None
    return None


def strength_reduce_ivs(commands, il_code):
    guard = 0
    changed = True
    while changed and guard < 20:
        changed = False
        guard += 1
        labels = {c.label_name(): i for i, c in enumerate(commands)
                  if c.label_name()}
        target_counts = {}
        for c in commands:
            for t in c.targets():
                target_counts[t] = target_counts.get(t, 0) + 1

        for back_idx, c in enumerate(commands):
            back_labels = [t for t in c.targets()
                           if labels.get(t, back_idx) < back_idx]
            if not back_labels:
                continue
            start_idx = labels[back_labels[0]]
            if target_counts.get(back_labels[0], 0) != 1:
                continue
            if not _single_block_loop_range(commands, start_idx, back_idx,
                                            labels):
                continue

            body = commands[start_idx + 1:back_idx]
            defined = set()
            def_count = {}
            def_of = {}
            for off, b in enumerate(body):
                bi = start_idx + 1 + off
                for o in b.outputs():
                    defined.add(o)
                    def_count[o] = def_count.get(o, 0) + 1
                    def_of[o] = bi

            # Basic IVs: iv updated by `t = iv + c; iv = t` (c literal), which
            # ShivyCX emits as Add(t, iv, c) then Set(iv, t). iv must be defined
            # exactly once in the loop (by that Set).
            basic_ivs = {}
            for b in body:
                if not isinstance(b, value_cmds.Set):
                    continue
                iv = b.output
                if def_count.get(iv) != 1:
                    continue
                if b.arg not in def_of:
                    continue
                add = commands[def_of[b.arg]]
                if not isinstance(add, math_cmds.Add):
                    continue
                inc = None
                if add.arg1 is iv and _lit(il_code, add.arg2) is not None:
                    inc = _lit(il_code, add.arg2)
                elif add.arg2 is iv and _lit(il_code, add.arg1) is not None:
                    inc = _lit(il_code, add.arg1)
                if inc is not None:
                    basic_ivs[iv] = inc
            if not basic_ivs:
                continue

            uses = _input_use_counts(commands)

            target = None
            for b in body:
                addr = None
                if isinstance(b, value_cmds.ReadAt):
                    addr = b.addr
                elif isinstance(b, value_cmds.SetAt):
                    addr = b.addr
                if addr is None or addr not in defined:
                    continue
                if def_count.get(addr) != 1:
                    continue
                chain = []
                res = _trace_stride(addr, def_of, commands, basic_ivs,
                                    defined, il_code, chain)
                if res is None or res[1] == 0:
                    continue
                iv, scale = res
                stride = scale * basic_ivs[iv]

                # Unique chain indices in program order.
                chain_idxs = sorted(set(chain))

                # The address chain is moved to the preheader, where it sees the
                # IV's loop-entry value, and the pointer is advanced at the loop
                # bottom. That is sound only if, within the body, the address is
                # computed BEFORE the IV is updated -- i.e. it uses the value
                # carried in from the previous iteration. If the IV is updated
                # first (as in a `v = *area; area += 8` va_arg fetch, where the
                # area pointer is bumped at the top of the body and the read
                # address is then derived from it), the loop-entry value is one
                # stride too low and the first iteration reads the wrong slot.
                # Require every chain command to precede the IV's in-loop update.
                iv_update_idx = def_of.get(iv)
                if iv_update_idx is None or (chain_idxs
                                             and max(chain_idxs) >= iv_update_idx):
                    continue

                chain_outputs = set()
                for ci in chain_idxs:
                    for o in commands[ci].outputs():
                        chain_outputs.add(o)
                # Each chain intermediate (except addr) must feed only the next
                # chain command (used exactly once), so the chain can be moved.
                ok = True
                for o in chain_outputs:
                    if o is addr:
                        continue
                    if uses.get(o, 0) != 1:
                        ok = False
                        break
                if not ok:
                    continue
                if uses.get(addr, 0) != 1:  # addr used only by this mem op
                    continue
                target = (addr, iv, stride, chain_idxs)
                break

            if target is None:
                continue

            addr, iv, stride, chain_idxs = target
            chain_id_set = set(chain_idxs)

            stride_val = ILValue(ctypes.longint)
            il_code.register_literal_var(stride_val, str(stride))
            # Advance via a temporary (t = addr + stride; addr = t) rather than
            # an in-place Add(addr, addr, stride): ShivyCX's liveness pass adds
            # inputs then removes outputs, so an in-place update looks like addr
            # is not live-in, and the allocator would clobber the pointer.
            adv_tmp = ILValue(addr.ctype)
            advance = [math_cmds.Add(adv_tmp, addr, stride_val),
                       value_cmds.Set(addr, adv_tmp)]
            chain_cmds = [commands[ci] for ci in chain_idxs]  # program order

            new_commands = []
            for i, cc in enumerate(commands):
                if i == start_idx:
                    new_commands.extend(chain_cmds)  # preheader
                    new_commands.append(cc)          # loop label
                    continue
                if i in chain_id_set:
                    continue  # remove chain from loop body
                if i == back_idx:
                    new_commands.extend(advance)     # advance before back-jump
                new_commands.append(cc)
            commands = new_commands
            changed = True
            break

    return commands


def optimize(commands, il_code):
    """Run all IL peephole passes for one function."""
    commands = simplify_arith(commands, il_code)
    commands = strength_reduce_divmod(commands, il_code)
    commands = hoist_loop_invariants(commands, il_code)
    commands = strength_reduce_ivs(commands, il_code)
    commands = fuse_compare_jumps(commands, il_code)
    return commands
