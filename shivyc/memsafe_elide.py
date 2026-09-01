"""Proving accesses safe so --mem-safe does not have to check them.

Uniform instrumentation checks every dereference, which costs roughly 30x on a
pointer-chasing loop. Most of those checks answer a question the compiler
already knows the answer to. Crust sees the whole call graph, so it can prove a
large share of accesses safe at compile time and emit nothing for them. Fil-C
cannot do this: its capability check is per-pointer at run time, with no
whole-program view to prove anything away.

Two rules, and they prove different halves of the problem.

**Rule 1 -- redundancy (local value numbering).** If the exact same address has
already been checked in this basic block with an access at least as wide, and
nothing since could have freed it, the second check is guaranteed to reach the
same verdict. This is what a read-modify-write costs today: `a[i] = i; s +=
a[i];` recomputes the address into a fresh ILValue, so the two accesses look
unrelated until you number the values. This rule needs no whole-program
information at all.

**Rule 2 -- provably in bounds.** An access at a *constant* offset into an
allocation of *statically known* size, which is provably live at that point, is
in bounds by arithmetic. Every struct field access through a `malloc(sizeof T)`
pointer is of this shape.

Rule 2 cannot remove the access outright, and the reason is worth stating
because it is easy to get wrong: a write check is not a pure predicate. It also
records which bytes are now defined, and that is the only way the runtime knows
an uninitialized read from an initialized one. Dropping a proved write made
`x->next = h` invisible to the shadow, so reading `h->next` afterwards was
reported as uninitialized -- a false positive manufactured by the optimization
meant to reduce noise. A proved write therefore becomes a bare shadow update
(`crust_ms_mark_init`) rather than nothing: the bounds and liveness work goes
away, the bookkeeping stays.

For the same reason rule 2 never elides a *read*. Bounds and liveness are
provable in advance; definedness is a run-time fact, and consulting it is the
whole of what a proved read still has to do.

Rule 1 has no such problem. It only fires when the identical address was
already checked at least as wide in the same block, so the bytes are already
known defined -- by the earlier write, or by the earlier read having passed.

The division of labour matters: the static pass (`--check-memory`) supplies the
temporal half of rule 2 -- which allocation a pointer targets and whether it is
still live -- via a real fixpoint dataflow over the CFG. It says nothing about
bounds. The spatial half is arithmetic done here, and only lands when the
offset is a literal. An `a[i]` with a runtime index is not provable by either,
which is why rule 1 carries the loop case and rule 2 carries the struct case.

Soundness note: IL values are not SSA -- the same ILValue is reassigned within
a function -- so nothing here is carried across a basic block boundary, and
value numbers are reissued on every definition. A stale fact would mean a
dropped check on an access that was not actually safe, which is the one error
this module must never make.
"""

import shivyc.il_cmds.compare as compare_cmds
import shivyc.il_cmds.control as control_cmds
import shivyc.il_cmds.math as math_cmds
import shivyc.il_cmds.value as value_cmds

from shivyc.memory_safety import ALLOCATORS


def _literal(val):
    """The integer a literal ILValue holds, or None."""
    lit = getattr(val, "literal", None)
    if lit is None:
        return None
    n = getattr(lit, "val", None)
    return n if isinstance(n, int) else None


def _alloc_size(cmd):
    """Statically known byte size of an allocator call, or None.

    Only the forms whose size is a literal count. `strdup` has no size operand
    and `realloc` of an unknown pointer is not worth the special case.
    """
    name = cmd.direct_name
    args = list(cmd.args)
    if name == "malloc" and len(args) >= 1:
        return _literal(args[0])
    if name == "calloc" and len(args) >= 2:
        a, b = _literal(args[0]), _literal(args[1])
        return a * b if a is not None and b is not None else None
    return None


def _access_size(cmd):
    ct = (cmd.output.ctype if isinstance(cmd, value_cmds.ReadAt)
          else cmd.val.ctype)
    n = getattr(ct, "size", None)
    return n if isinstance(n, int) and n > 0 else None


def _natural_loops(cfg, dom):
    """Back edges, as (header, latch, body blocks, preheader).

    A back edge is an edge whose target dominates its source. The body is
    everything the header dominates that can still reach the latch; the
    preheader is the header's single predecessor from outside the loop, which
    is the only place a hoisted statement can go and be executed exactly once.
    """
    n = len(cfg.blocks)
    preds = {}
    for b in range(n):
        preds[b] = set()
    for b in range(n):
        for sc in cfg.succ[b]:
            preds[sc].add(b)

    loops = []
    for latch in range(n):
        for h in cfg.succ[latch]:
            if h not in dom.get(latch, ()):
                continue                      # not a back edge
            body = set([h, latch])
            stack = [latch]
            while stack:
                b = stack.pop()
                for p in preds[b]:
                    if p not in body and h in dom.get(p, ()):
                        body.add(p)
                        stack.append(p)
            outside = [p for p in preds[h] if p not in body]
            pre = outside[0] if len(outside) == 1 else None
            loops.append((h, latch, body, pre))
    return loops


def _dominators(cfg):
    """Block -> set of blocks that dominate it (itself included).

    Needed because a loop guard only tells you something about the blocks it
    actually controls. `i < n` on one arm of a branch says nothing on the
    other, and an access in a block merely *reachable* from the comparison may
    also be reachable by a path that skipped it.
    """
    n = len(cfg.blocks)
    if n == 0:
        return {}
    preds = {}
    for b in range(n):
        preds[b] = set()
    for b in range(n):
        for sc in cfg.succ[b]:
            preds[sc].add(b)

    everything = frozenset(range(n))
    dom = {0: frozenset([0])}
    for b in range(1, n):
        dom[b] = everything

    changed = True
    while changed:
        changed = False
        for b in range(1, n):
            if not preds[b]:
                new = frozenset([b])
            else:
                acc = None
                for p in preds[b]:
                    acc = dom[p] if acc is None else (acc & dom[p])
                new = (acc or frozenset()) | frozenset([b])
            if new != dom[b]:
                dom[b] = new
                changed = True
    return dom


def _upper_bounds(cfg, cmds, dom):
    """Block -> {id(ILValue): exclusive upper bound}, from dominating guards.

    Recognizes the shape the front end emits for a counted loop: a comparison
    into a temporary, then a conditional jump on it. Only the arm on which the
    comparison held is credited with the fact.
    """
    out = {}
    for b in range(len(cfg.blocks)):
        out[b] = {}

    for g in range(len(cfg.blocks)):
        start, end = cfg.blocks[g]
        if end <= start:
            continue
        jump = cmds[end - 1]
        if not isinstance(jump, control_cmds._GeneralJump):
            continue

        # The comparison feeding this jump, if it is the obvious one.
        cmp_cmd = None
        for i in range(end - 2, start - 1, -1):
            c = cmds[i]
            if (isinstance(c, compare_cmds._GeneralCmp)
                    and getattr(c, "fuse", None) is None
                    and c.output is jump.cond):
                cmp_cmd = c
                break
            if jump.cond in c.outputs():
                break            # produced by something else; not a guard
        if cmp_cmd is None:
            continue

        bound = _literal(cmp_cmd.arg2)
        if bound is None:
            continue
        if isinstance(cmp_cmd, compare_cmds.LessCmp):
            limit = bound                      # i < bound
        elif isinstance(cmp_cmd, compare_cmds.LessOrEqCmp):
            limit = bound + 1                  # i <= bound
        else:
            continue

        # JumpZero branches away when the comparison was false, so the
        # comparison holds on the fallthrough; JumpNotZero is the mirror.
        tgt_blk = cfg.block_of.get(cfg.labels.get(jump.label, -1))
        fall = cfg.block_of.get(end) if end < len(cmds) else None
        if isinstance(jump, control_cmds.JumpZero):
            true_blk = fall
        else:
            true_blk = tgt_blk
        if true_blk is None:
            continue

        key = id(cmp_cmd.arg1)
        for b in range(len(cfg.blocks)):
            if true_blk in dom.get(b, ()):
                prev = out[b].get(key)
                out[b][key] = limit if prev is None else min(prev, limit)
    return out


def _nonneg(cmds):
    """ILValues that cannot be negative anywhere in the function.

    A loop guard gives only the upper half of a range. The lower half comes
    from how the variable is written: if every assignment to it is either a
    non-negative constant or an increase by a non-negative constant, it never
    goes below zero, whatever path is taken. Checking every definition in the
    function means no control-flow reasoning is needed for this half -- and a
    single unexplained assignment anywhere disqualifies the value, which is
    what makes it safe.
    """
    defs = {}
    for c in cmds:
        for o in c.outputs():
            defs.setdefault(id(o), []).append(c)

    cand = set(defs)
    changed = True
    while changed:
        changed = False
        for key in list(cand):
            for c in defs[key]:
                ok = False
                if isinstance(c, value_cmds.Set):
                    lit = _literal(c.arg)
                    ok = (lit is not None and lit >= 0) or id(c.arg) in cand
                elif isinstance(c, math_cmds.Add):
                    for p, q in ((c.arg1, c.arg2), (c.arg2, c.arg1)):
                        k = _literal(q)
                        if k is not None and k >= 0 and id(p) in cand:
                            ok = True
                            break
                elif isinstance(c, math_cmds.Mult):
                    ok = (id(c.arg1) in cand and id(c.arg2) in cand)
                if not ok:
                    cand.discard(key)
                    changed = True
                    break
    # Literals are self-evidently in range and have no definition at all.
    return cand


def _single_def_origins(cmds, sizes, fn):
    """Constant-offset origins for values defined exactly once.

    Block-local tracking cannot see the `malloc` that happens before a loop,
    which is precisely the base pointer every indexed access inside the loop
    is built from. A value assigned once in the whole function holds the same
    thing wherever it is live, so its origin is safe to carry across blocks.
    Anything reassigned -- `p = p->next` -- is excluded and stays block-local.
    """
    count = {}
    for c in cmds:
        for o in c.outputs():
            count[id(o)] = count.get(id(o), 0) + 1

    origin = {}
    for i in range(len(cmds)):
        c = cmds[i]
        if (isinstance(c, control_cmds.Call) and c.direct_name in ALLOCATORS
                and c.ret is not None and count.get(id(c.ret)) == 1
                and (fn, i) in sizes):
            origin[id(c.ret)] = ((fn, i), 0)
        elif isinstance(c, value_cmds.Set) and count.get(id(c.output)) == 1:
            if id(c.arg) in origin:
                origin[id(c.output)] = origin[id(c.arg)]
        elif isinstance(c, math_cmds.Add) and count.get(id(c.output)) == 1:
            for p, q in ((c.arg1, c.arg2), (c.arg2, c.arg1)):
                if id(p) in origin:
                    k = _literal(q)
                    if k is not None:
                        al, off = origin[id(p)]
                        origin[id(c.output)] = (al, off + k)
                    break
    return origin


class _Numbering:
    """Local value numbering for one basic block.

    Two ILValues get the same number exactly when they are computed from the
    same operation over the same numbers. Literals are numbered by their value,
    not their identity, so the `4` in one address computation matches the `4`
    in the next -- without that, two identical `a[i]` address computations
    never line up, because the front end materializes a separate literal for
    each.
    """

    def __init__(self):
        self.num = {}          # id(ILValue) -> value number
        self.avail = {}        # expression key -> value number
        self.lits = {}         # literal integer -> value number
        self.next = 1

    def fresh(self):
        n = self.next
        self.next += 1
        return n

    def of(self, val):
        """The current number for `val`, assigning one if it is unknown."""
        lit = _literal(val)
        if lit is not None:
            if lit not in self.lits:
                self.lits[lit] = self.fresh()
            return self.lits[lit]
        key = id(val)
        if key not in self.num:
            self.num[key] = self.fresh()
        return self.num[key]

    def define(self, out, key=None):
        """Number a freshly defined value, reusing `key`'s number if seen.

        Redefinition always issues a new number rather than mutating the old
        one, so any expression recorded earlier keeps referring to the value
        that was live when it was recorded. That is what makes this safe on
        non-SSA IL.
        """
        if key is not None and key in self.avail:
            self.num[id(out)] = self.avail[key]
            return
        n = self.fresh()
        self.num[id(out)] = n
        if key is not None:
            self.avail[key] = n


def _is_block_end(cmd):
    return isinstance(cmd, (control_cmds.Jump, control_cmds.Return,
                            control_cmds._GeneralJump))


def safe_accesses(il_code, symbol_table):
    """Decide which checks can be dropped or downgraded.

    Returns (skip, mark_only, n_redundant, n_bounds):
      skip      {function: set of command indices needing no check at all}
      mark_only {function: set of write indices needing only a shadow update}
    """
    import shivyc.memory_safety as memory_safety

    prog = memory_safety._program_from_il(il_code, symbol_table)
    analyzer = memory_safety.Analyzer(prog)
    analyzer.run()
    live_access = analyzer.live_access

    # allocation id -> statically known size, and the ILValue holding its base
    sizes = {}
    bases = {}
    base_at = {}
    for fn in prog.functions:
        cmds = prog.functions[fn]
        for i in range(len(cmds)):
            c = cmds[i]
            if (isinstance(c, control_cmds.Call)
                    and c.direct_name in ALLOCATORS and c.ret is not None):
                n = _alloc_size(c)
                if n is not None:
                    sizes[(fn, i)] = n
                    bases[(fn, i)] = c.ret
                    base_at[(fn, i)] = i

    out = {}
    marks = {}
    hoists = {}
    n_redundant = 0
    n_bounds = 0
    n_ranged = 0
    n_hoisted = 0

    for fn in prog.functions:
        cmds = prog.functions[fn]
        live = live_access.get(fn, {})
        skip = set()

        # Rule 3 machinery: loop-carried ranges.
        cfg = memory_safety.CFG(cmds)
        dom = _dominators(cfg)
        bounds = _upper_bounds(cfg, cmds, dom)
        nonneg = _nonneg(cmds)
        far_origin = _single_def_origins(cmds, sizes, fn)

        mark = set()
        indexed_at = {}    # cmd index -> (alloc, base offset, index, scale, n)
        vn = _Numbering()
        checked = {}       # value number -> widest size already checked
        # id(ILValue) -> (allocation id, constant offset from its base)
        origin = {}
        # id(ILValue) -> (index ILValue, scale) for `i * k`
        scaled = {}
        # id(ILValue) -> (allocation id, base offset, index ILValue, scale)
        indexed = {}

        for i in range(len(cmds)):
            c = cmds[i]

            blk = cfg.block_of.get(i, 0)

            if isinstance(c, control_cmds.Label) or _is_block_end(c):
                # Nothing survives a block boundary: a value number means
                # "computed above, in this straight line", and a predecessor
                # reaching here by another path proves none of it.
                vn = _Numbering()
                checked = {}
                origin = {}
                scaled = {}
                indexed = {}
                continue

            if isinstance(c, control_cmds.Call):
                # A call may free or realloc anything reachable, so every
                # earlier proof about liveness lapses. Bounds facts about a
                # *constant* offset would survive, but the allocation they
                # refer to might not, so they go too.
                if c.direct_name in ALLOCATORS and c.ret is not None:
                    vn.define(c.ret)
                    if (fn, i) in sizes:
                        origin[id(c.ret)] = ((fn, i), 0)
                else:
                    if c.ret is not None:
                        vn.define(c.ret)
                    origin = {}
                    indexed = {}
                checked = {}
                continue

            if isinstance(c, value_cmds.ReadAt):
                n = _access_size(c)
                before_r = len(skip)
                if n is not None and _try_elide(
                        c, i, n, False, vn, checked, origin, live, sizes,
                        skip, mark, indexed, bounds.get(blk, {}), nonneg,
                        far_origin):
                    if len(skip) != before_r:
                        n_ranged += 1
                    n_redundant += 1
                vn.define(c.output)
                continue

            if isinstance(c, value_cmds.SetAt):
                n = _access_size(c)
                if n is not None:
                    before = len(mark)
                    if _try_elide(c, i, n, True, vn, checked, origin,
                                  live, sizes, skip, mark, indexed,
                                  bounds.get(blk, {}), nonneg, far_origin):
                        n_redundant += 1
                    elif len(mark) != before:
                        n_bounds += 1
                        idx_info = indexed.get(id(c.addr))
                        if idx_info is not None:
                            indexed_at[i] = idx_info + (n,)
                        else:
                            # A constant-offset proved write inside a loop
                            # touches the same bytes on every iteration, so one
                            # update before the loop covers all of them. Same
                            # hoist, with a fixed span -- this is what a struct
                            # field assigned in a loop looks like.
                            ori2 = origin.get(id(c.addr))
                            if ori2 is None:
                                ori2 = far_origin.get(id(c.addr))
                            if ori2 is not None:
                                indexed_at[i] = (ori2[0], ori2[1], None, 0, n)
                continue

            _propagate(c, vn, origin, scaled, indexed, far_origin)

        # Hoist whole-loop shadow updates. A proved write inside a counted
        # loop defines one contiguous run of bytes over the whole loop, so a
        # single update before the loop replaces one per iteration -- which,
        # once bounds and liveness are proved away, is the entire remaining
        # cost of the loop. Runs before the results are stored, since a hoisted
        # write moves from the "downgrade" set into the "skip" set.
        if mark:
            done = _hoist_marks(cfg, dom, cmds, fn, mark, indexed_at, bounds,
                                nonneg, sizes, bases, base_at, hoists)
            n_hoisted += len(done)
            n_bounds -= len(done)
            # A hoisted write needs nothing at all at its own site: bounds and
            # liveness were proved, and the definedness update now happens once
            # before the loop.
            mark = mark - done
            skip = skip | done

        if skip:
            out[fn] = skip
        if mark:
            marks[fn] = mark

    return out, marks, hoists, n_redundant, n_bounds, n_ranged, n_hoisted


def _hoist_marks(cfg, dom, cmds, fn, mark, indexed_at, bounds, nonneg,
                 sizes, bases, base_at, hoists):
    """Replace per-iteration shadow updates with one before the loop.

    Returns the set of write indices the loop-level update covers.

    Three conditions, all necessary:

    * the write runs on **every** iteration -- its block must dominate the
      latch, or a write under an `if` inside the loop would have bytes marked
      defined that it never wrote;
    * the base pointer is computed **before** the loop, so naming it in the
      preheader is legal;
    * the loop has a single preheader, which is the only place a statement runs
      exactly once before the loop.

    Marking the whole run costs precision when a loop exits early: bytes that
    were never written are treated as defined. That is a missed report rather
    than a false one, the same direction the escape rule already trades in.
    """
    done = set()
    loops = _natural_loops(cfg, dom)

    def climb(blk, al):
        """Move an insertion point outward through enclosing loops.

        The preheader of an inner loop still sits inside the outer one, so a
        nested loop's update would run once per outer iteration -- on the
        obvious two-level benchmark that left the shadow update executing two
        thousand times and most of the overhead in place. The update depends
        only on the base pointer and a constant span, so it is invariant in
        every enclosing loop whose preheader the allocation precedes.
        """
        moved = True
        while moved:
            moved = False
            for _h, _l, body2, pre2 in loops:
                if blk in body2 and pre2 is not None:
                    # Against the insertion point, not the block start: the
                    # outermost preheader is usually the entry block, and the
                    # allocation lives *inside* it. Comparing with the start
                    # said "the base is not available yet" and the update
                    # stayed one loop too deep, still running once per outer
                    # iteration.
                    insert_at = cfg.blocks[pre2][1] - 1
                    if base_at.get(al, len(cmds)) < insert_at:
                        blk = pre2
                        moved = True
                        break
        return blk

    for h, latch, body, pre in loops:
        if pre is None:
            continue
        for i in sorted(mark):
            if i in done:
                continue
            blk = cfg.block_of.get(i)
            if blk not in body or latch not in dom or blk not in dom[latch]:
                continue
            info = indexed_at.get(i)
            if info is None:
                continue
            al, off, index, scale, n = info
            size = sizes.get(al)
            base = bases.get(al)
            if size is None or base is None:
                continue
            if index is None:
                span = n                    # same bytes every iteration
            else:
                limit = bounds.get(blk, {}).get(id(index))
                if (limit is None or id(index) not in nonneg
                        or scale <= 0 or limit < 1):
                    continue
                span = (limit - 1) * scale + n
            # The allocation must happen before the preheader, or its base is
            # not a value that exists where the hoisted update would run.
            if base_at.get(al, len(cmds)) >= cfg.blocks[pre][1] - 1:
                continue
            if off < 0 or off + span > size:
                continue
            target = climb(pre, al)
            _t_start, t_end = cfg.blocks[target]
            hoists.setdefault(fn, []).append(
                (t_end - 1, base, off, span, cmds[i].r))
            done.add(i)
    return done


def _try_elide(c, i, n, writing, vn, checked, origin, live, sizes, skip, mark,
               indexed, bounds, nonneg, far_origin):
    """Judge command `i`. Returns True when its check is dropped entirely.

    A rule-2 write is added to `mark` instead and returns False: it still has
    to update the definedness shadow, just not test anything.
    """
    num = vn.of(c.addr)
    prev = checked.get(num)

    # Rule 1: an identical address already checked at least this wide. The
    # earlier check settled bounds, liveness *and* definedness for these
    # bytes, so nothing is lost by dropping this one.
    if prev is not None and prev >= n:
        skip.add(i)
        return True

    checked[num] = max(prev or 0, n)

    # Rule 3: a variable index whose range is bounded on both sides. The guard
    # of the enclosing loop supplies the upper bound and the way the counter is
    # written supplies the lower one, so `a[i]` inside `for (i = 0; i < N; i++)`
    # is in bounds for every iteration when N * scale fits the allocation.
    #
    # Writes are downgraded rather than removed here for exactly the reason
    # rule 2 is: the check is also what records definedness.
    idx = indexed.get(id(c.addr))
    if idx is not None:
        al, off, index, scale = idx
        size = sizes.get(al)
        limit = bounds.get(id(index))
        if (size is not None and limit is not None and live.get(i) == al
                and id(index) in nonneg and scale > 0 and off >= 0
                and limit >= 1
                and off + (limit - 1) * scale + n <= size):
            if writing:
                mark.add(i)
            else:
                skip.add(i)
            return not writing

    # Rule 2: constant offset into a live allocation of known size. Applies to
    # writes only -- see the module docstring for why a proved read still has
    # to run.
    if not writing:
        return False
    ori = origin.get(id(c.addr))
    if ori is not None:
        al, off = ori
        size = sizes.get(al)
        if (size is not None and live.get(i) == al
                and off >= 0 and off + n <= size):
            mark.add(i)
    return False


def _base_origin(val, origin, far_origin):
    """Origin of `val`, preferring the block-local fact over the global one."""
    ori = origin.get(id(val))
    return ori if ori is not None else far_origin.get(id(val))


def _propagate(c, vn, origin, scaled, indexed, far_origin):
    """Number an ordinary command's result and track how addresses are built."""
    if isinstance(c, value_cmds.Set):
        vn.define(c.output, ("set", vn.of(c.arg)))
        ori = _base_origin(c.arg, origin, far_origin)
        if ori is not None:
            origin[id(c.output)] = ori
        else:
            origin.pop(id(c.output), None)
        if id(c.arg) in scaled:
            scaled[id(c.output)] = scaled[id(c.arg)]
        if id(c.arg) in indexed:
            indexed[id(c.output)] = indexed[id(c.arg)]
        return

    if isinstance(c, math_cmds.Add):
        a, b = c.arg1, c.arg2
        vn.define(c.output, ("add", min(vn.of(a), vn.of(b)),
                             max(vn.of(a), vn.of(b))))
        origin.pop(id(c.output), None)
        indexed.pop(id(c.output), None)
        for p, q in ((a, b), (b, a)):
            ori = _base_origin(p, origin, far_origin)
            if ori is None:
                continue
            al, off = ori
            k = _literal(q)
            if k is not None:                       # base + constant
                origin[id(c.output)] = (al, off + k)
                return
            sc = scaled.get(id(q))
            if sc is not None:                      # base + index * scale
                indexed[id(c.output)] = (al, off, sc[0], sc[1])
                return
            # base + an unscaled variable: an index of stride one.
            indexed[id(c.output)] = (al, off, q, 1)
            return
        return

    if isinstance(c, math_cmds.Mult):
        vn.define(c.output, ("mult", min(vn.of(c.arg1), vn.of(c.arg2)),
                             max(vn.of(c.arg1), vn.of(c.arg2))))
        origin.pop(id(c.output), None)
        scaled.pop(id(c.output), None)
        for p, q in ((c.arg1, c.arg2), (c.arg2, c.arg1)):
            k = _literal(q)
            if k is not None and k > 0 and _literal(p) is None:
                scaled[id(c.output)] = (p, k)
                break
        return

    if isinstance(c, math_cmds.Subtr):
        vn.define(c.output, ("sub", vn.of(c.arg1), vn.of(c.arg2)))
        origin.pop(id(c.output), None)
        indexed.pop(id(c.output), None)
        scaled.pop(id(c.output), None)
        return

    for o in c.outputs():
        vn.define(o)
        origin.pop(id(o), None)
        indexed.pop(id(o), None)
        scaled.pop(id(o), None)
