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

    # allocation id -> statically known size
    sizes = {}
    for fn in prog.functions:
        cmds = prog.functions[fn]
        for i in range(len(cmds)):
            c = cmds[i]
            if (isinstance(c, control_cmds.Call)
                    and c.direct_name in ALLOCATORS and c.ret is not None):
                n = _alloc_size(c)
                if n is not None:
                    sizes[(fn, i)] = n

    out = {}
    marks = {}
    n_redundant = 0
    n_bounds = 0

    for fn in prog.functions:
        cmds = prog.functions[fn]
        live = live_access.get(fn, {})
        skip = set()

        mark = set()
        vn = _Numbering()
        checked = {}       # value number -> widest size already checked
        # id(ILValue) -> (allocation id, constant offset from its base)
        origin = {}

        for i in range(len(cmds)):
            c = cmds[i]

            if isinstance(c, control_cmds.Label) or _is_block_end(c):
                # Nothing survives a block boundary: a value number means
                # "computed above, in this straight line", and a predecessor
                # reaching here by another path proves none of it.
                vn = _Numbering()
                checked = {}
                origin = {}
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
                checked = {}
                continue

            if isinstance(c, value_cmds.ReadAt):
                n = _access_size(c)
                if n is not None and _try_elide(c, i, n, False, vn, checked,
                                                origin, live, sizes, skip,
                                                mark):
                    n_redundant += 1
                vn.define(c.output)
                continue

            if isinstance(c, value_cmds.SetAt):
                n = _access_size(c)
                if n is not None:
                    before = len(mark)
                    if _try_elide(c, i, n, True, vn, checked, origin,
                                  live, sizes, skip, mark):
                        n_redundant += 1
                    elif len(mark) != before:
                        n_bounds += 1
                continue

            _propagate(c, vn, origin)

        if skip:
            out[fn] = skip
        if mark:
            marks[fn] = mark

    return out, marks, n_redundant, n_bounds


def _try_elide(c, i, n, writing, vn, checked, origin, live, sizes, skip, mark):
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


def _propagate(c, vn, origin):
    """Number an ordinary command's result and track constant offsets."""
    if isinstance(c, value_cmds.Set):
        vn.define(c.output, ("set", vn.of(c.arg)))
        if id(c.arg) in origin:
            origin[id(c.output)] = origin[id(c.arg)]
        else:
            origin.pop(id(c.output), None)
        return

    if isinstance(c, math_cmds.Add):
        a, b = c.arg1, c.arg2
        vn.define(c.output, ("add", min(vn.of(a), vn.of(b)),
                             max(vn.of(a), vn.of(b))))
        # base + literal keeps a known offset; base + anything else does not.
        for p, q in ((a, b), (b, a)):
            if id(p) in origin:
                k = _literal(q)
                if k is not None:
                    al, off = origin[id(p)]
                    origin[id(c.output)] = (al, off + k)
                    return
        origin.pop(id(c.output), None)
        return

    if isinstance(c, math_cmds.Mult):
        vn.define(c.output, ("mult", min(vn.of(c.arg1), vn.of(c.arg2)),
                             max(vn.of(c.arg1), vn.of(c.arg2))))
        origin.pop(id(c.output), None)
        return

    if isinstance(c, math_cmds.Subtr):
        vn.define(c.output, ("sub", vn.of(c.arg1), vn.of(c.arg2)))
        origin.pop(id(c.output), None)
        return

    for o in c.outputs():
        vn.define(o)
        origin.pop(id(o), None)
