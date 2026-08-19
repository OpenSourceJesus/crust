"""Register-partitioned thread context switching for ShivyCX.

A function header may declare which functions run as bare-metal threads, and on
which side of a two-way register split each one lives:

    void main()
    assert worker_a in threads.left(core=0)
    assert worker_b in threads.right(core=0)
    { worker_a(); worker_b(); }

`extensions.py` records these as {fn: {'side','core'}}. This module turns that,
plus the whole-program call graph and each function's *actual* post-allocation
register footprint, into:

  1. a per-thread register footprint (union over the thread's transitive call
     graph),
  2. a partition of the GP and XMM register files into a `left` budget and a
     `right` budget that are disjoint where the footprints allow it, and
  3. a *specialized* context switcher: because the two groups use disjoint
     registers, the left->right and right->left routines each save/restore only
     their own group's registers, with no runtime test of "which kind of thread
     is current". A left thread can only have left registers live, so that is
     all the switch must preserve.

The partition the analysis computes can also be fed back into the register
allocator (see asm_gen's per-function `alloc_registers` hook) so the real code
generation is constrained to each thread's budget, making the disjointness --
and therefore the minimal switcher -- a guarantee rather than an observation.
"""

import sys
import shivyc.spots as spots

# Targets for which the whole pipeline below -- register pool, asm scanner,
# allocation budget and switcher emitter -- actually exists.
SUPPORTED_TARGETS = ["x86_64", "arm64"]

# ---------------------------------------------------------------------------
# Per-target register model
# ---------------------------------------------------------------------------
# Three things differ per target and everything else in this file is shared:
# which registers the allocator hands out, how to recognise one in emitted
# assembly, and what a save/restore looks like.
#
# x86-64: the allocator draws value homes from the caller-saved pool in
# spots.registers. Nine registers, and the ABI's argument registers are in
# there, so a two-way split leaves roughly four a side.
#
# AArch64: value homes come from the *callee-saved* range x19-x28 (see
# _arm64_function in asm_gen). Ten registers, none of them argument or
# scratch, which is why a two-way split is more comfortable here than on
# x86 -- five a side rather than four, and no contention with the ABI.
# x29/x30 are frame pointer and link register and are never allocatable;
# x0-x18 are caller-saved scratch the allocator does not use for homes.

# GP allocation pool, in the allocator's preferred order.
GP_POOL = [r.name for r in spots.registers]          # rax,rcx,rdx,rsi,rdi,r8,r9,r10,r11
XMM_POOL = [x.name for x in spots.xmm_arg_regs]       # xmm0..xmm7

ARM64_GP_POOL = ["x%d" % n for n in range(19, 29)]    # x19..x28
ARM64_FP_POOL = ["d%d" % n for n in range(8, 16)]     # d8..d15, callee-saved

# Map every sub-register spelling back to its 64-bit owner (eax->rax, etc).
_SUB_TO_R64 = {}
for _r64, _names in spots.RegSpot.reg_map.items():
    for _nm in _names:
        if _nm:
            _SUB_TO_R64[_nm] = _r64

# AArch64's equivalent: w19 is the low half of x19, and the assembler spells
# it that way wherever a 32-bit operation is emitted, so a scan that only
# looked for `x19` would miss most integer code.
_ARM64_SUB_TO_X = {}
for _n in range(0, 31):
    _ARM64_SUB_TO_X["x%d" % _n] = "x%d" % _n
    _ARM64_SUB_TO_X["w%d" % _n] = "x%d" % _n
# The FP/SIMD file is one register seen through several widths.
_ARM64_SUB_TO_D = {}
for _n in range(0, 32):
    for _pfx in ("d", "s", "h", "q", "v"):
        _ARM64_SUB_TO_D["%s%d" % (_pfx, _n)] = "d%d" % _n


def gp_pool(target):
    return ARM64_GP_POOL if target == "arm64" else GP_POOL


def fp_pool(target):
    return ARM64_FP_POOL if target == "arm64" else XMM_POOL


def transitive_closure(edges, root):
    """All functions reachable from `root` (inclusive) via direct-call edges."""
    seen, stack = set(), [root]
    while stack:
        fn = stack.pop()
        if fn in seen:
            continue
        seen.add(fn)
        stack.extend(edges.get(fn, ()))
    return seen


def scan_asm_registers_arm64(asm_text, full=False):
    """Scan emitted AArch64 asm, returning {func: {'gp': set, 'xmm': set}}.

    Same job as the x86 scanner and a different parse. Three differences
    matter:

    * Register names carry the *width* as a prefix -- `w19` is the low half of
      `x19` -- so a scan looking only for `x`-names would miss most integer
      code, which ShivyCX emits as 32-bit `w` operations wherever the C type
      is `int`.
    * Only x19-x28 are value homes here (see ARM64_GP_POOL). x0-x18 appear
      constantly as scratch and as argument registers, and counting them would
      make every function's footprint look like the whole register file.
    * There is no `QWORD PTR` noise, but `[x19, #8]` addressing means register
      names appear inside brackets, so the operand text is scanned whole
      rather than split on commas.
    """
    import re
    import sys
    tok = re.compile(r"\b([a-z][a-z0-9]*)\b")
    funcs = {}
    cur = None
    globals_seen = set()
    # Two different questions, and they have different answers.
    #
    #   full=False  which *callee-saved* registers the thread uses. This is
    #               what a cooperative switch must preserve: that switch is a
    #               call, so caller-saved registers are already dead across it
    #               by the ABI and the compiler has spilled anything live.
    #
    #   full=True   every register the thread uses. This is what a *preemptive*
    #               switch must preserve, because a timer interrupt lands at an
    #               arbitrary instruction and x0-x18 are holding live values at
    #               that moment. Saving only the callee-saved bank there would
    #               corrupt the interrupted thread -- silently, and only
    #               sometimes, depending on where the tick fell.
    home_set = {}
    if full:
        for _n in range(0, 29):          # x0-x28; x29 is FP, sp is separate
            home_set["x%d" % _n] = 1
        home_set["x30"] = 1              # link register is live data too
    else:
        for _r in ARM64_GP_POOL:
            home_set[_r] = 1
    for raw in asm_text.splitlines():
        line = raw.strip()
        if line.startswith(".global"):
            globals_seen.add(line.split()[1])
            continue
        if line.endswith(":") and not line.startswith("."):
            label = line[:-1].strip()
            cur = label if label in globals_seen else cur
            funcs.setdefault(cur, {"gp": set(), "xmm": set()})
            continue
        if cur is None:
            continue
        parts = line.split(None, 1)
        operands = parts[1] if len(parts) > 1 else ""
        # See the note in scan_asm_registers: regex findall is outside the
        # translator's subset, so this only runs on the host.
        if sys.implementation.name != "shivyc":
            for t in tok.findall(operands):
                if t in _ARM64_SUB_TO_X:
                    x = _ARM64_SUB_TO_X[t]
                    if x in home_set:
                        funcs[cur]["gp"].add(x)
                elif t in _ARM64_SUB_TO_D:
                    funcs[cur]["xmm"].add(_ARM64_SUB_TO_D[t])
    funcs.pop(None, None)
    return funcs


def scan_for_target(asm_text, target, full=False):
    if target == "arm64":
        return scan_asm_registers_arm64(asm_text, full=full)
    return scan_asm_registers(asm_text)


def scan_asm_registers(asm_text):
    """Scan emitted Intel-syntax asm, returning {func: {'gp': set, 'xmm': set}}.

    Register usage is read per function body (delimited by `name:` labels that
    follow a `.global name`), normalising sub-registers to their 64-bit owner.
    This is the real, post-register-allocation footprint.
    """
    import re
    import sys
    tok = re.compile(r"\b([a-z][a-z0-9]*)\b")
    funcs = {}
    cur = None
    globals_seen = set()
    for raw in asm_text.splitlines():
        line = raw.strip()
        if line.startswith(".global"):
            globals_seen.add(line.split()[1])
            continue
        if line.endswith(":") and not line.startswith("."):
            label = line[:-1].strip()
            cur = label if label in globals_seen else cur
            funcs.setdefault(cur, {"gp": set(), "xmm": set()})
            continue
        if cur is None:
            continue
        # only look at the operand text, after the mnemonic
        parts = line.split(None, 1)
        operands = parts[1] if len(parts) > 1 else ""
        # The asm token scan uses regex .findall with a \b pattern, which is
        # outside the translator's regex subset. This whole driver only runs on
        # the host (it shells out to a compiler), so guard it: under the
        # self-hosted build the condition folds to false and the block is
        # dropped; under CPython it runs normally.
        if sys.implementation.name != "shivyc":
            for t in tok.findall(operands):
                if t in _SUB_TO_R64:
                    r64 = _SUB_TO_R64[t]
                    if r64 in GP_POOL:
                        funcs[cur]["gp"].add(r64)
                elif t.startswith("xmm"):
                    funcs[cur]["xmm"].add(t)
    funcs.pop(None, None)
    return funcs


class ThreadPlan:
    """Result of the thread-partition analysis."""

    def __init__(self):
        self.threads = {}        # fn -> {'side','core'}
        self.members = {}        # fn -> set(transitively-called functions)
        self.left_funcs = set()
        self.right_funcs = set()
        self.shared = set()      # functions reachable from both sides
        self.left_gp = set()
        self.right_gp = set()
        self.left_xmm = set()
        self.right_xmm = set()
        self.gp_budget = {"left": [], "right": []}
        self.xmm_budget = {"left": [], "right": []}
        self.gp_overlap = []     # regs that must be saved on a cross switch
        self.xmm_overlap = []
        self.target = "x86_64"   # which register model this plan describes
        # Full per-thread footprints, including caller-saved. Only meaningful
        # on targets where the allocator draws homes from the callee-saved
        # range (arm64); on x86 the pool already is the caller-saved set, so
        # these mirror left_gp/right_gp.
        self.left_full = set()
        self.right_full = set()


def _split_budget(pool, need_left, need_right):
    """Carve `pool` into disjoint left/right budgets covering the needs.

    Left takes from the front, right from the back, so the two budgets stay
    disjoint. If the combined need exceeds the pool, the budgets meet in the
    middle and the shared tail is reported as overlap (must be saved on a
    cross-group switch).
    """
    n = len(pool)
    if need_left + need_right <= n:
        left = pool[:need_left]
        right = pool[n - need_right:] if need_right else []
        return left, right, []
    # Over-subscribed: proportional split, overlap = intersection.
    cut = round(n * need_left / (need_left + need_right)) if (need_left + need_right) else 0
    cut = max(0, min(n, cut))
    left = pool[:max(cut, need_left)]
    right = pool[n - max(n - cut, need_right):]
    overlap = sorted(set(left) & set(right), key=pool.index)
    return left, right, overlap


def analyze(threads, edges, func_regs, target="x86_64"):
    """Build a ThreadPlan from thread declarations, call edges and footprints.

    threads   - {fn: {'side','core'}}
    edges     - {caller: set(callees)} whole-program call graph
    func_regs - {fn: {'gp': set, 'xmm': set}} post-allocation footprints
    target    - which register pool to split
    """
    plan = ThreadPlan()
    plan.threads = dict(threads)

    for fn, rec in threads.items():
        members = transitive_closure(edges, fn)
        plan.members[fn] = members
        if rec["side"] == "left":
            plan.left_funcs |= members
        else:
            plan.right_funcs |= members

    plan.shared = plan.left_funcs & plan.right_funcs

    def footprint(funcs, key):
        out = set()
        for f in funcs:
            out |= func_regs.get(f, {}).get(key, set())
        return out

    plan.left_gp = footprint(plan.left_funcs, "gp")
    plan.right_gp = footprint(plan.right_funcs, "gp")
    plan.left_xmm = footprint(plan.left_funcs, "xmm")
    plan.right_xmm = footprint(plan.right_funcs, "xmm")
    plan.left_full = footprint(plan.left_funcs, "full") or set(plan.left_gp)
    plan.right_full = footprint(plan.right_funcs, "full") or set(plan.right_gp)

    lg, rg, og = _split_budget(gp_pool(target),
                               len(plan.left_gp), len(plan.right_gp))
    lx, rx, ox = _split_budget(fp_pool(target),
                               len(plan.left_xmm), len(plan.right_xmm))
    plan.target = target
    plan.gp_budget = {"left": lg, "right": rg}
    plan.xmm_budget = {"left": lx, "right": rx}
    plan.gp_overlap = og
    plan.xmm_overlap = ox
    return plan


def allocation_budgets(plan):
    """{fn: {'gp':[...], 'xmm':[...]}} restricting each thread function to its
    group's register budget, for feeding back into the allocator. Shared
    functions get the intersection (so they are safe to call from either side).
    """
    out = {}
    inter_gp = [r for r in gp_pool(plan.target)
                if r in plan.gp_budget["left"] and r in plan.gp_budget["right"]]
    inter_xmm = [x for x in fp_pool(plan.target)
                 if x in plan.xmm_budget["left"] and x in plan.xmm_budget["right"]]
    for fn in plan.left_funcs | plan.right_funcs:
        if fn in plan.shared:
            out[fn] = {"gp": inter_gp or plan.gp_budget["left"],
                       "xmm": inter_xmm or plan.xmm_budget["left"]}
        elif fn in plan.left_funcs:
            out[fn] = {"gp": plan.gp_budget["left"], "xmm": plan.xmm_budget["left"]}
        else:
            out[fn] = {"gp": plan.gp_budget["right"], "xmm": plan.xmm_budget["right"]}
    return out


def _a64_save_pairs(regs, base, off0):
    """Emit stp/ldp pairs saving `regs` at [base, #off0...]. Returns lines.

    AArch64 stores two registers per instruction, so a four-register bank is
    two instructions rather than x86's four -- the split makes the switch
    cheap, and the ISA then halves it again. An odd count ends with a single
    `str`.
    """
    lines = []
    i = 0
    off = off0
    while i + 1 < len(regs):
        lines.append("    stp %s, %s, [%s, #%d]" % (regs[i], regs[i + 1],
                                                    base, off))
        i += 2
        off += 16
    if i < len(regs):
        lines.append("    str %s, [%s, #%d]" % (regs[i], base, off))
    return lines


def _a64_restore_pairs(regs, base, off0):
    lines = []
    i = 0
    off = off0
    while i + 1 < len(regs):
        lines.append("    ldp %s, %s, [%s, #%d]" % (regs[i], regs[i + 1],
                                                    base, off))
        i += 2
        off += 16
    if i < len(regs):
        lines.append("    ldr %s, [%s, #%d]" % (regs[i], base, off))
    return lines


def generate_switcher_arm64(plan):
    """Emit the specialized context switcher as AArch64 assembly.

    Same contract as the x86 version and a different shape. Points worth
    stating:

    * Arguments are x0 (outgoing TCB) and x1 (incoming), per AAPCS64, rather
      than rdi/rsi.
    * The return address is in **x30** (the link register), not on the stack.
      So the "where do we resume" slot is saved from a register, and restoring
      it plus `ret` is the whole return path -- there is no stack read as on
      x86.
    * `sp` cannot be an operand of a general move, so it goes via a scratch
      register. x9 is used: it is caller-saved scratch (x0-x18) and outside
      the x19-x28 home pool, so it can never belong to either thread's
      footprint.

    TCB layout matches the x86 one so a TCB is portable across both:
        +0   saved sp
        +8   saved x30 (resume address)
        +16.. saved group registers, in footprint order
    """
    left_gp = [r for r in gp_pool(plan.target) if r in plan.left_gp]
    right_gp = [r for r in gp_pool(plan.target) if r in plan.right_gp]

    L = []
    L.append("/* Generated by ShivyCX thread_contracts: register-")
    L.append("   partitioned context switcher (left/right groups), AArch64. */")
    L.append("    .section .text")

    for name, save, restore in (("switch_to_right", left_gp, right_gp),
                                ("switch_to_left", right_gp, left_gp)):
        L.append("")
        L.append("    .global %s" % name)
        L.append("%s:" % name)
        L.append("    /* save %d reg(s): %s */"
                 % (len(save), ", ".join(save) if save else "(none)"))
        L.extend(_a64_save_pairs(save, "x0", 16))
        L.append("    mov x9, sp")
        L.append("    str x9, [x0, #0]")
        L.append("    str x30, [x0, #8]")
        L.append("    /* restore %d reg(s): %s */"
                 % (len(restore), ", ".join(restore) if restore else "(none)"))
        L.extend(_a64_restore_pairs(restore, "x1", 16))
        L.append("    ldr x9, [x1, #0]")
        L.append("    mov sp, x9")
        L.append("    ldr x30, [x1, #8]")
        L.append("    ret")

    L.append("")
    return "\n".join(L) + "\n"


def generate_switcher(plan):
    """Emit the specialized context switcher as GNU-as Intel-syntax asm.

    Each direction saves exactly the *footprint* of the outgoing thread's group
    and restores the footprint of the incoming group -- always correct, however
    the registers were allocated. When the footprints are made disjoint (via the
    constrained-allocation feedback), the two save-sets share nothing and the
    routines are minimal. There is no runtime test of thread kind: each
    direction's register list is baked in.

    TCB layout (per thread), 8-byte slots unless noted:
        +0   saved rsp
        +8   saved resume address
        +16.. saved group GP registers (footprint order), then XMM (16B each)
    """
    left_gp = [r for r in gp_pool(plan.target) if r in plan.left_gp]
    right_gp = [r for r in gp_pool(plan.target) if r in plan.right_gp]
    left_xmm = [x for x in fp_pool(plan.target) if x in plan.left_xmm]
    right_xmm = [x for x in fp_pool(plan.target) if x in plan.right_xmm]

    lines = []
    lines.append("/* Generated by ShivyCX thread_contracts: register-")
    lines.append("   partitioned context switcher (left/right groups). */")
    lines.append("    .intel_syntax noprefix")
    lines.append("    .section .text")

    def emit(name, save_gp, save_xmm, restore_gp, restore_xmm):
        lines.append("    .global " + name)
        lines.append(name + ":")
        off = 16
        for r in save_gp:
            lines.append(f"    mov QWORD PTR [rdi+{off}], {r}")
            off += 8
        for x in save_xmm:
            lines.append(f"    movdqu XMMWORD PTR [rdi+{off}], {x}")
            off += 16
        lines.append("    mov QWORD PTR [rdi+0], rsp")
        lines.append("    mov rax, [rsp]")          # resume address (pushed by call)
        lines.append("    mov QWORD PTR [rdi+8], rax")
        off = 16
        for r in restore_gp:
            lines.append(f"    mov {r}, QWORD PTR [rsi+{off}]")
            off += 8
        for x in restore_xmm:
            lines.append(f"    movdqu {x}, XMMWORD PTR [rsi+{off}]")
            off += 16
        lines.append("    mov rsp, QWORD PTR [rsi+0]")
        lines.append("    ret")
        lines.append("")

    emit("switch_to_right", left_gp, left_xmm, right_gp, right_xmm)
    emit("switch_to_left", right_gp, right_xmm, left_gp, left_xmm)

    lines.append("    .section .note.GNU-stack,\"\",@progbits")
    return "\n".join(lines) + "\n"


def _a64_pair_run(run, base, pair_op, single_op):
    """Emit `run` (a list of (index, register)) as paired accesses.

    `run` holds entries whose TCB slots are consecutive, so every adjacent
    couple is eight bytes apart and can go in one `stp`/`ldp`. An odd tail
    falls back to the single-register form.
    """
    out = []
    i = 0
    while i + 1 < len(run):
        i0, r0 = run[i]
        i1, r1 = run[i + 1]
        out.append("    %s  %s, %s, [%s, #%d]"
                   % (pair_op, r0, r1, base, 24 + i0 * 8))
        i += 2
    if i < len(run):
        i0, r0 = run[i]
        out.append("    %s  %s, [%s, #%d]"
                   % (single_op, r0, base, 24 + i0 * 8))
    return out


def generate_preempt_switcher_arm64(plan):
    """Emit the partition-aware *preemptive* timer path for AArch64.

    This is an alternative body for the EL1h IRQ vector slot (VBAR_EL1 +
    0x280). The generic entry in baremetal64/vectors_arm64.S saves a fixed
    twenty-two registers on every tick because it cannot know what the
    interrupted code was using. Here the compiler does know, so each side saves
    only its own group's footprint.

    Three requirements, each a correctness matter rather than an optimisation:

    * **The full footprint is saved, not the callee-saved bank.** A cooperative
      switch is a call, so caller-saved registers are dead across it by the
      ABI. A timer interrupt lands at an arbitrary instruction, where x0-x18
      hold live values. Saving only x19-x28 would corrupt the interrupted
      thread intermittently, depending where the tick fell.

    * **The resume address is ELR_EL1 and the flags are SPSR_EL1.** `eret`
      restores from both. Preserving registers but not SPSR resumes the thread
      with someone else's condition flags and interrupt mask.

    * **On entry there are no free registers.** Every scratch register the ISR
      wants may belong to the interrupted thread. The first version of this
      emitter did `adrp x9, cur_tcb` to find the TCB -- destroying x9 before
      saving it, so the thread's x9 was replaced by a pointer -- and used
      x11-x13 for the queue swap without saving them at all. Both are the kind
      of corruption that only shows up when a tick happens to land while those
      registers are live.

      The fix is the standard one: push two registers to the stack first, so
      there are two provably free scratch registers, and use *only* those for
      the whole routine. SP_EL1 is valid at EL1h, so the stack is available
      before anything else is.

    Selection is data, not a branch: `timer_vector` holds the currently
    specialized entry and each side flips it to the other, so the next tick is
    already specialized.

    Slot usage: the 128-byte vector entry only needs a branch to
    `timer_dispatch`, so these bodies live out of line -- the same structure as
    exc_common in vectors_arm64.S.

    TCB layout, 8-byte slots:
        +0  saved sp    +8  saved ELR_EL1    +16 saved SPSR_EL1
        +24.. saved group registers, one slot each in footprint order
    """
    order = ["x%d" % n for n in range(0, 29)] + ["x30"]
    left = [r for r in order if r in plan.left_full]
    right = [r for r in order if r in plan.right_full]

    # The two scratch registers. They are saved to the stack before anything
    # else, so they are free for the rest of the routine whether or not the
    # interrupted thread was using them.
    S0, S1 = "x9", "x10"

    def slot_of(regs, r):
        return 24 + regs.index(r) * 8

    def emit_side(name, save, other, nxt):
        L = []
        L.append("    .global %s" % name)
        L.append("%s:" % name)
        L.append("    /* Entered from timer_dispatch, which has already pushed")
        L.append("       the original %s/%s to [sp]/[sp+8]. They are free" % (S0, S1))
        L.append("       scratch here; their saved values are recovered below.")
        L.append("       Do not branch here directly. */")
        L.append("")
        L.append("    /* %s = current TCB */" % S0)
        L.append("    adrp %s, cur_tcb" % S0)
        L.append("    add  %s, %s, :lo12:cur_tcb" % (S0, S0))
        L.append("    ldr  %s, [%s]" % (S0, S0))
        L.append("")
        L.append("    /* save %d reg(s) of the running group: %s */"
                 % (len(save), ", ".join(save) if save else "(none)"))
        # Pair adjacent saves. TCB slots are assigned by list index, so two
        # consecutive entries are always eight bytes apart and `stp` can take
        # both -- halving the instruction count for the same registers.
        #
        # This is not a micro-optimisation, it is the difference between the
        # partition paying for itself and not. Measured before pairing: the
        # partitioned ISR saved 13 registers in 70 instructions while the
        # save-all baseline saved 31 in 75, because the baseline was paired and
        # this was not. Fewer registers in barely fewer instructions is no win
        # at all.
        #
        # The scratch pair is excluded here (it is still on the stack and is
        # recovered below), which can split the list into runs; pairing is done
        # within each run so a gap never produces a `stp` across a hole.
        run = []
        for idx, r in enumerate(save):
            if r in (S0, S1):
                L.extend(_a64_pair_run(run, S0, "stp", "str"))
                run = []
                continue
            run.append((idx, r))
        L.extend(_a64_pair_run(run, S0, "stp", "str"))
        # Now recover the two scratch registers' original values from the
        # stack and place them in the TCB, using the other scratch as the
        # carrier. This is why exactly two are freed rather than one.
        for i, r in enumerate((S0, S1)):
            if r in save:
                L.append("    ldr  %s, [sp, #%d]   /* original %s */"
                         % (S1, i * 8, r))
                L.append("    str  %s, [%s, #%d]" % (S1, S0, slot_of(save, r)))
        L.append("    add  sp, sp, #16")
        L.append("")
        L.append("    /* sp, resume address and flags */")
        L.append("    mov  %s, sp" % S1)
        L.append("    str  %s, [%s, #0]" % (S1, S0))
        L.append("    mrs  %s, elr_el1" % S1)
        L.append("    str  %s, [%s, #8]" % (S1, S0))
        L.append("    mrs  %s, spsr_el1" % S1)
        L.append("    str  %s, [%s, #16]" % (S1, S0))
        L.append("")
        L.append("    /* Acknowledge the tick at the board's timer and")
        L.append("       interrupt controller. The ISR cannot do this itself:")
        L.append("       rearming CNTP_TVAL and the EOI protocol differ per")
        L.append("       board (GICv2 on virt and Jetson, the BCM local")
        L.append("       controller on a Pi), so the image supplies")
        L.append("       `void timer_ack(void)`. Without it the timer stays")
        L.append("       asserted and the next eret re-enters immediately.")
        L.append("")
        L.append("       Safe to call here: the outgoing thread's registers are")
        L.append("       already in its TCB, so the callee may clobber freely,")
        L.append("       and we are still on its stack. */")
        L.append("    bl   timer_ack")
        L.append("")
        L.append("    /* Re-derive the outgoing TCB pointer. %s is caller-saved,"
                 % S0)
        L.append("       so the call above was entitled to destroy it -- and")
        L.append("       does. The swap below reads it as the outgoing thread,")
        L.append("       so without this reload it stores garbage into")
        L.append("       next_tcb and the *following* switch resumes from a")
        L.append("       corrupt TCB: eret lands at EL0 on a misaligned PC.")
        L.append("       cur_tcb still names the outgoing thread here, because")
        L.append("       the swap has not happened yet. */")
        L.append("    adrp %s, cur_tcb" % S0)
        L.append("    add  %s, %s, :lo12:cur_tcb" % (S0, S0))
        L.append("    ldr  %s, [%s]" % (S0, S0))
        L.append("")
        L.append("    /* swap cur/next and select %s for the next tick */" % nxt)
        L.append("    /* Exchange cur_tcb and next_tcb. Both halves matter:")
        L.append("       setting cur = next but leaving next alone makes the")
        L.append("       second tick switch the incoming thread to itself, so")
        L.append("       the outgoing one never runs again. That looks like a")
        L.append("       working scheduler for exactly one switch.")
        L.append("")
        L.append("       Two scratch registers are one short for a three-way")
        L.append("       exchange, so the outgoing pointer is parked on the")
        L.append("       outgoing thread's stack, which is still current. */")
        L.append("    sub  sp, sp, #16")
        L.append("    str  %s, [sp]" % S0)             # park outgoing
        L.append("    adrp %s, next_tcb" % S0)
        L.append("    add  %s, %s, :lo12:next_tcb" % (S0, S0))
        L.append("    ldr  %s, [%s]" % (S1, S0))       # S1 = incoming
        L.append("    str  %s, [sp, #8]" % S1)         # park incoming
        L.append("    ldr  %s, [sp]" % S1)             # S1 = outgoing
        L.append("    str  %s, [%s]" % (S1, S0))       # next = outgoing
        L.append("    adrp %s, cur_tcb" % S0)
        L.append("    add  %s, %s, :lo12:cur_tcb" % (S0, S0))
        L.append("    ldr  %s, [sp, #8]" % S1)         # S1 = incoming
        L.append("    str  %s, [%s]" % (S1, S0))       # cur = incoming
        L.append("    add  sp, sp, #16")
        L.append("    adrp %s, timer_vector" % S0)
        L.append("    add  %s, %s, :lo12:timer_vector" % (S0, S0))
        L.append("    adrp %s, %s" % (S1, nxt))
        L.append("    add  %s, %s, :lo12:%s" % (S1, S1, nxt))
        L.append("    str  %s, [%s]" % (S1, S0))
        L.append("    /* next_tcb is the scheduler's to update, not ours */")
        L.append("")
        L.append("    /* restore %d reg(s) of the incoming group: %s */"
                 % (len(other), ", ".join(other) if other else "(none)"))
        L.append("    adrp %s, cur_tcb" % S0)
        L.append("    add  %s, %s, :lo12:cur_tcb" % (S0, S0))
        L.append("    ldr  %s, [%s]" % (S0, S0))       # S0 = incoming TCB
        L.append("    ldr  %s, [%s, #16]" % (S1, S0))
        L.append("    msr  spsr_el1, %s" % S1)
        L.append("    ldr  %s, [%s, #8]" % (S1, S0))
        L.append("    msr  elr_el1, %s" % S1)
        L.append("    ldr  %s, [%s, #0]" % (S1, S0))
        L.append("    mov  sp, %s" % S1)
        # Restore everything except the scratch pair, then the pair last --
        # after which no scratch is available, so nothing may follow but eret.
        run = []
        for idx, r in enumerate(other):
            if r in (S0, S1):
                L.extend(_a64_pair_run(run, S0, "ldp", "ldr"))
                run = []
                continue
            run.append((idx, r))
        L.extend(_a64_pair_run(run, S0, "ldp", "ldr"))
        if S1 in other:
            L.append("    ldr  %s, [%s, #%d]" % (S1, S0, slot_of(other, S1)))
        if S0 in other:
            L.append("    ldr  %s, [%s, #%d]" % (S0, S0, slot_of(other, S0)))
        L.append("    eret")
        L.append("")
        return L

    L = []
    L.append("/* Generated by ShivyCX thread_contracts: partition-aware")
    L.append("   preemptive timer switcher (AArch64, EL1h IRQ vector).")
    L.append("   Branch to timer_dispatch from VBAR_EL1 + 0x280. */")
    L.append("    .section .text")
    L.append("")
    L.append("    .global timer_dispatch")
    L.append("timer_dispatch:")
    L.append("    /* Indirect through timer_vector: the running side's entry is")
    L.append("       already selected, so there is no test of thread kind.")
    L.append("")
    L.append("       Reaching an indirect branch needs a register, and on entry")
    L.append("       there are none free -- so this parks BOTH scratch registers")
    L.append("       on the stack first and the entries below inherit that")
    L.append("       frame. Doing the save in each entry instead would clobber")
    L.append("       x9 here and then dutifully save the clobbered value. */")
    L.append("    sub  sp, sp, #16")
    L.append("    stp  x9, x10, [sp]")
    L.append("    adrp x9, timer_vector")
    L.append("    add  x9, x9, :lo12:timer_vector")
    L.append("    ldr  x9, [x9]")
    L.append("    br   x9")
    L.append("")

    L.extend(emit_side("timer_isr_left", left, right, "timer_isr_right"))
    L.extend(emit_side("timer_isr_right", right, left, "timer_isr_left"))

    L.append("    .section .data")
    L.append("    .balign 8")
    L.append("    .global timer_vector")
    L.append("timer_vector:")
    L.append("    .quad timer_isr_left      /* seeded to the left side */")
    L.append("    .global cur_tcb")
    L.append("cur_tcb:")
    L.append("    .quad 0")
    L.append("    .global next_tcb")
    L.append("next_tcb:")
    L.append("    .quad 0")
    L.append("")
    return "\n".join(L) + "\n"


def generate_preempt_switcher(plan):
    """Emit the partition-aware *preemptive* timer path as GNU-as asm.

    Installed at IDT vector 32 (IRQ0) in place of the generic irq0 stub, this
    saves only the running thread's group footprint instead of the full
    interrupt_frame64. The trick: left and right footprints are disjoint, so the
    left timer ISR can use the right registers as scratch (they are dead for a
    left thread) and vice-versa -- no extra spills, and no runtime test of which
    kind of thread is current.

    Control transfers through `timer_dispatch` (the gate target), which does
    `jmp [timer_vector]`. Each ISR flips `timer_vector` to the other side before
    returning, so the next tick is already specialized -- the selection is data,
    never a branch on thread kind.

    TCB layout (per thread), 8-byte slots:
        +0  saved rsp     +8  saved rip     +16 saved rflags
        +24.. saved group GP regs (footprint order), then XMM (16B each)

    Scheduler globals it expects (defined here in .bss/.data):
        cur_tcb, next_tcb : pointers to the running / other thread's TCB
        timer_vector      : current specialized ISR (seeded to timer_isr_left)
    """
    left_gp = [r for r in gp_pool(plan.target) if r in plan.left_gp]
    right_gp = [r for r in gp_pool(plan.target) if r in plan.right_gp]
    left_xmm = [x for x in fp_pool(plan.target) if x in plan.left_xmm]
    right_xmm = [x for x in fp_pool(plan.target) if x in plan.right_xmm]

    CS, SS = "0x08", "0x10"
    L = []
    L.append("/* Generated by ShivyCX thread_contracts: partition-aware")
    L.append("   preemptive timer switcher (IRQ0). Saves only the running")
    L.append("   group's footprint. Install timer_dispatch at IDT[32]. */")
    L.append("    .intel_syntax noprefix")
    L.append("    .section .text")
    L.append("    .global timer_dispatch")
    L.append("timer_dispatch:")
    L.append("    jmp QWORD PTR [rip+timer_vector]")
    L.append("")

    def emit(name, save_gp, save_xmm, rest_gp, rest_xmm, other, scratch, base, frame):
        # scratch: regs free during SAVE phase (incoming group, dead for outgoing)
        # base/frame: regs free during RESTORE phase (outgoing group, already saved)
        s0, s1 = scratch[0], scratch[1]
        L.append("    .global " + name)
        L.append(name + ":")
        L.append(f"    mov {s0}, QWORD PTR [rip+cur_tcb]")
        off = 24
        for r in save_gp:
            L.append(f"    mov QWORD PTR [{s0}+{off}], {r}")
            off += 8
        for x in save_xmm:
            L.append(f"    movdqu XMMWORD PTR [{s0}+{off}], {x}")
            off += 16
        # CPU iret frame at [rsp]: rip, cs, rflags, rsp, ss
        L.append(f"    mov {s1}, [rsp+0]")
        L.append(f"    mov QWORD PTR [{s0}+8], {s1}     /* rip */")
        L.append(f"    mov {s1}, [rsp+16]")
        L.append(f"    mov QWORD PTR [{s0}+16], {s1}    /* rflags */")
        L.append(f"    mov {s1}, [rsp+24]")
        L.append(f"    mov QWORD PTR [{s0}+0], {s1}     /* rsp */")
        # PIC EOI (al/dx belong to the outgoing group -> already saved, free)
        L.append("    mov al, 0x20")
        L.append("    mov dx, 0x20")
        L.append("    out dx, al")
        # flip dispatch target for the next tick (no test next time)
        L.append(f"    lea {s1}, [rip+{other}]")
        L.append(f"    mov QWORD PTR [rip+timer_vector], {s1}")
        # Swap, reading old-cur FRESH from memory (cur_tcb is still valid here):
        # the save-phase base may alias al/dx clobbered by the EOI above, so we
        # do not rely on it surviving.
        L.append(f"    mov {s1}, QWORD PTR [rip+cur_tcb]   /* old cur */")
        L.append(f"    mov {base}, QWORD PTR [rip+next_tcb] /* incoming */")
        L.append(f"    mov QWORD PTR [rip+next_tcb], {s1}   /* next = old cur */")
        L.append(f"    mov QWORD PTR [rip+cur_tcb], {base}  /* cur = incoming */")
        off = 24
        for r in rest_gp:
            L.append(f"    mov {r}, QWORD PTR [{base}+{off}]")
            off += 8
        for x in rest_xmm:
            L.append(f"    movdqu {x}, XMMWORD PTR [{base}+{off}]")
            off += 16
        # build an iret frame on the incoming thread's stack and iretq
        f0, f1, f2 = frame
        L.append(f"    mov {f0}, QWORD PTR [{base}+0]      /* incoming rsp */")
        L.append(f"    mov {f1}, QWORD PTR [{base}+8]      /* incoming rip */")
        L.append(f"    mov {f2}, QWORD PTR [{base}+16]     /* incoming rflags */")
        L.append(f"    sub {f0}, 40")
        L.append(f"    mov QWORD PTR [{f0}+0], {f1}")
        L.append(f"    mov QWORD PTR [{f0}+8], {CS}")
        L.append(f"    mov QWORD PTR [{f0}+16], {f2}")
        L.append(f"    mov {f1}, QWORD PTR [{base}+0]")
        L.append(f"    mov QWORD PTR [{f0}+24], {f1}")
        L.append(f"    mov QWORD PTR [{f0}+32], {SS}")
        L.append(f"    mov rsp, {f0}")
        L.append("    iretq")
        L.append("")

    # left running: scratch = right regs; restore-phase base/frame = left regs
    emit("timer_isr_left", left_gp, left_xmm, right_gp, right_xmm,
         "timer_isr_right", scratch=right_gp[:2] or ["r8", "r9"],
         base=left_gp[0], frame=left_gp[1:4])
    # right running: scratch = left regs; base/frame = right regs
    emit("timer_isr_right", right_gp, right_xmm, left_gp, left_xmm,
         "timer_isr_left", scratch=left_gp[:2] or ["rax", "rcx"],
         base=right_gp[0], frame=right_gp[1:4])

    L.append("    .section .data")
    L.append("    .align 8")
    L.append("    .global timer_vector")
    L.append("timer_vector: .quad timer_isr_left")
    L.append("    .section .bss")
    L.append("    .align 8")
    L.append("    .global cur_tcb")
    L.append("cur_tcb: .skip 8")
    L.append("    .global next_tcb")
    L.append("next_tcb: .skip 8")
    L.append("    .section .note.GNU-stack,\"\",@progbits")
    return "\n".join(L) + "\n"


def format_report(plan):
    """Human-readable summary of the partition and switch cost."""
    def fmt(s, order):
        return ", ".join(r for r in order if r in s) or "(none)"
    L = []
    L.append("ShivyCX thread partition")
    L.append("========================")
    for fn, rec in sorted(plan.threads.items()):
        L.append(f"  thread {fn}: side={rec['side']} core={rec['core']} "
                 f"(call graph: {len(plan.members[fn])} fns)")
    L.append("")
    L.append(f"left  call graph : {len(plan.left_funcs)} fns")
    L.append(f"right call graph : {len(plan.right_funcs)} fns")
    if plan.shared:
        L.append(f"shared functions : {', '.join(sorted(plan.shared))}")
    else:
        L.append("shared functions : (none - call graphs are disjoint)")
    L.append("")
    gpp = gp_pool(plan.target)
    fpp = fp_pool(plan.target)
    L.append(f"left  GP footprint : {fmt(plan.left_gp, gpp)}")
    L.append(f"right GP footprint : {fmt(plan.right_gp, gpp)}")
    L.append(f"left  GP budget    : {', '.join(plan.gp_budget['left']) or '(none)'}")
    L.append(f"right GP budget    : {', '.join(plan.gp_budget['right']) or '(none)'}")
    if plan.left_xmm or plan.right_xmm:
        L.append(f"left  FP  footprint: {fmt(plan.left_xmm, fpp)}")
        L.append(f"right FP  footprint: {fmt(plan.right_xmm, fpp)}")
    L.append("")
    naive = len(gpp) + len(fpp)                     # save-everything switch
    cross_l = len(plan.left_gp) + len(plan.left_xmm)
    cross_r = len(plan.right_gp) + len(plan.right_xmm)
    overlap = (plan.left_gp & plan.right_gp) | (plan.left_xmm & plan.right_xmm)
    if overlap:
        L.append(f"footprints overlap on: {', '.join(sorted(overlap))} "
                 "(shared regs saved on each side; constrained allocation can "
                 "push these apart)")
    else:
        L.append("footprints are disjoint: left and right share no registers")
    L.append(f"context switch saves: left->right {cross_l} regs, "
             f"right->left {cross_r} regs (vs {naive} for save-all)")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Driver: collect declarations, scan real footprints, constrain, emit switcher.
# ---------------------------------------------------------------------------
def _collect_threads(files):
    """Union of threads.left/right declarations across all .c inputs."""
    import shivyc.extensions as extensions
    threads = {}
    for f in files:
        if not f.endswith(".c"):
            continue
        try:
            _, info = extensions.preprocess_extensions(open(f).read())
        except Exception:
            continue
        threads.update(info.threads)
    return threads


def _compile_and_scan(files, budget_json=None, target="x86_64"):
    """Compile each .c (optionally with a register budget) and scan the emitted
    asm, returning the merged {func: {'gp','xmm'}} footprint map."""
    import os
    import shutil
    import subprocess
    import sys
    import tempfile

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    func_regs = {}
    workdir = tempfile.mkdtemp(prefix="shivycx_threads_")
    try:
        for f in files:
            if not f.endswith(".c"):
                continue
            base = os.path.basename(f)
            cpy = os.path.join(workdir, base)
            shutil.copyfile(f, cpy)
            cmd = [sys.executable, "-m", "shivyc.main", cpy, "-c",
                   "-o", cpy[:-2] + ".o"]
            # Without this the driver compiled x86 no matter what the caller
            # asked for, then scanned x86 asm and emitted an x86 switcher --
            # the silent-wrong-output bug this whole path had.
            cmd += ["--target", target]
            # Resolve includes (e.g. py2c's `#include "shivyc_rt.h"`) against the
            # original file's directory, so generated C -- not just standalone
            # hand-written C -- can be compiled and scanned for its footprint.
            srcdir = os.path.dirname(os.path.abspath(f))
            if srcdir:
                cmd += ["-I", srcdir]
            if budget_json:
                cmd += ["--thread-alloc-json", budget_json]
            env = dict(os.environ)
            if target == "arm64":
                # ShivyCX hands AArch64 assembly to the *host* assembler
                # unless told to use our own, which fails on an x86 host. The
                # failure used to be swallowed by the `continue` below, so
                # every footprint came back empty and the partition reported
                # "no registers used" for code that plainly used registers.
                env["SHIVYC_RASM"] = "1"
                env["SHIVYC_RLINK"] = "1"
            r = subprocess.run(cmd, cwd=repo_root, capture_output=True,
                               text=True, env=env)
            s_path = cpy[:-2] + ".s"
            if r.returncode != 0 or not os.path.exists(s_path):
                # A file that will not compile cannot contribute a footprint,
                # and silently treating it as "uses no registers" produces a
                # partition that looks clean and is meaningless. Say so.
                sys.stderr.write(
                    "shivyc: thread analysis could not compile %s for "
                    "--target %s; its register footprint is unknown, so the "
                    "partition below would be wrong.\n%s%s\n"
                    % (base, target, r.stdout, r.stderr))
                raise SystemExit(1)
            asm_txt = open(s_path).read()
            for fn, regs in scan_for_target(asm_txt, target).items():
                slot = func_regs.setdefault(fn, {"gp": set(), "xmm": set(),
                                                 "full": set()})
                slot["gp"] |= regs["gp"]
                slot["xmm"] |= regs["xmm"]
            # Second view: everything the thread touches, for the preemptive
            # path. Same asm, different question -- see scan_asm_registers_arm64.
            for fn, regs in scan_for_target(asm_txt, target,
                                            full=True).items():
                slot = func_regs.setdefault(fn, {"gp": set(), "xmm": set(),
                                                 "full": set()})
                slot["full"] |= regs["gp"]
        return func_regs
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run(files, args):
    """End-to-end thread-partition driver behind --emit-thread-switcher.

    1. collect threads.left/right declarations,
    2. build the whole-program call graph,
    3. scan each thread's real (post-allocation) register footprint,
    4. compute the partition; feed disjoint budgets back into the allocator and
       re-scan so the footprints become disjoint,
    5. emit the specialized switcher and print a before/after report.
    """
    import json
    import os
    import sys
    import tempfile
    import shivyc.callgraph as callgraph
    from shivyc.errors import error_collector

    # Every stage below is x86-64 specific: GP_POOL comes from the x86 spots
    # table, scan_asm_registers parses Intel syntax and normalises via an
    # x86 sub-register map, and generate_switcher emits `mov QWORD PTR`.
    # None of that is guarded by the target, and _compile_and_scan used to
    # shell out to the compiler *without* passing --target -- so asking for
    # arm64 silently produced a byte-identical x86 switcher, reporting
    # `rax, rcx, rdx, rsi` as an AArch64 footprint. Wrong output that looks
    # plausible is worse than no output, so unsupported targets stop here.
    target = getattr(args, "target", "x86_64") or "x86_64"
    if target not in SUPPORTED_TARGETS:
        sys.stderr.write(
            "shivyc: --emit-thread-switcher does not support --target %s "
            "yet.\n"
            "  Supported: %s\n"
            "  The register partition, the asm scanner and the switcher are "
            "per-target;\n"
            "  emitting the x86-64 switcher here would be silently wrong.\n"
            % (target, ", ".join(sorted(SUPPORTED_TARGETS))))
        return 1

    threads = _collect_threads(files)
    if not threads:
        print("no threads.left/right declarations found in inputs")
        return 1

    graph, _ = callgraph.build_program_graph(files, args)
    error_collector.clear()

    # Pass 1: unconstrained footprints.
    fr0 = _compile_and_scan(files, target=target)
    plan0 = analyze(threads, graph.edges, fr0, target=target)

    print(format_report(plan0))
    print()

    # Pass 2: feed disjoint budgets back into the allocator, re-scan. This
    # round-trips budgets through a temp JSON file and re-invokes the compiler,
    # so it is host-only (no tempfile/subprocess in the self-hosted build).
    budgets = allocation_budgets(plan0)
    flat = {fn: b["gp"] for fn, b in budgets.items()}
    if sys.implementation.name != "shivyc":
        bj = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(flat, bj)
        bj.close()
        try:
            fr1 = _compile_and_scan(files, budget_json=bj.name,
                                    target=target)
        finally:
            os.unlink(bj.name)
    else:
        fr1 = fr0
    plan1 = analyze(threads, graph.edges, fr1, target=target)

    print("=== after constrained re-allocation ===")
    print(format_report(plan1))

    out = args.emit_thread_switcher
    with open(out, "w") as fh:
        if target == "arm64":
            fh.write(generate_switcher_arm64(plan1))
        else:
            fh.write(generate_switcher(plan1))
    # Also emit the partition-aware preemptive timer path (IRQ0).
    preempt = out[:-2] + ".preempt.s" if out.endswith(".s") else out + ".preempt.s"
    with open(preempt, "w") as fh:
        if target == "arm64":
            fh.write(generate_preempt_switcher_arm64(plan1))
        else:
            fh.write(generate_preempt_switcher(plan1))
    gp_full = len(gp_pool(target))
    print()
    print(f"wrote cooperative switcher: {out}")
    print(f"wrote preemptive timer path: {preempt}")
    if target == "arm64":
        # The preemptive path saves the *full* footprint, not the callee-saved
        # bank the cooperative switcher uses: a tick lands at an arbitrary
        # instruction, so caller-saved registers are live. Reporting the
        # cooperative number here would understate what the ISR does.
        print(f"  timer ISR saves the running group's full footprint "
              f"(left {len(plan1.left_full)}, right {len(plan1.right_full)} "
              f"GP regs) instead of all 30 on every tick")
        print(f"  (the cooperative switcher saves only the callee-saved bank: "
              f"left {len(plan1.left_gp)}, right {len(plan1.right_gp)})")
    else:
        print(f"  timer ISR saves the running group's footprint "
              f"(left {len(plan1.left_gp)}, right {len(plan1.right_gp)} GP "
              f"regs) instead of all {gp_full} on every tick")
    return 0
