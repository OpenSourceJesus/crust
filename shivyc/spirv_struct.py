"""CFG structurizer for the SPIR-V back end (SPIRV.md section 6.2).

Pure graph code: no SPIR-V in here. It takes a function's basic blocks and
returns a tree of structured constructs (if / loop / break / continue), or
raises Unstructurable when the CFG needs something this pass does not do. The
caller then falls back to the dispatcher loop, which handles any CFG.

Input
    kinds[i]  'ret' | 'jmp' | 'cond' | 'fall'
    succs[i]  [] for ret, [target] for jmp/fall, [taken, fall] for cond
    Block 0 is the entry.

Output: a Seq, i.e. a list of items
    ('block', i)                  emit the straight-line body of block i
    ('ret', i)                    emit block i's return
    ('br', 'break'|'continue'|'merge')
                                  leave the innermost loop / go to its
                                  continue target / go to the innermost
                                  if-construct's merge block
    ('if', i, then, else, merged) block i ends in a conditional branch;
                                  `merged` is False when both arms leave
    ('loop', i, body, exits)      natural loop headed by block i; `exits` is
                                  False when nothing leaves it by `break`

The rules it keeps, because SPIR-V does (the validator in spirv_reader.py
checks them independently):
  * a construct may only be left through its own merge block, a `break` or
    `continue` of the innermost loop, or a return;
  * a loop has one merge block, so a loop with two different exit targets is
    not handled here;
  * a block is emitted exactly once -- there is no tail duplication, so a
    join that is not the immediate post-dominator of a branch (switch
    fall-through, `goto` into the middle of something) is not handled here.
"""


class Unstructurable(Exception):
    pass


class _Loop:
    def __init__(self, header, body, exit_block):
        self.header = header
        self.body = body            # set of block indices, includes header
        self.exit = exit_block      # the single block after the loop, or None
        self.parent = None
        self.children = []


DUP_LIMIT = 12      # a join block this small may be copied into each arm
DUP_DEPTH = 6


def structurize(kinds, succs, sizes=None):
    """sizes[i] is the number of IL commands in block i; when given, tiny
    single-exit blocks reached from two nesting levels (the join of a
    short-circuit `a && b`) are copied instead of refused."""
    s = _Structurizer(kinds, succs, sizes)
    return s.run()


class _Structurizer:
    def __init__(self, kinds, succs, sizes=None):
        self.sizes = sizes
        self.depth = 0
        self.kinds = kinds
        self.succs = succs
        self.n = len(kinds)
        self.preds = [[] for _i in range(self.n)]
        self.emitted = {}
        self.loop_of_header = {}
        self.inner = []             # block -> innermost _Loop or None

    # ------------------------------------------------------------ analysis

    def run(self):
        self._check_shape()
        self._reachable()
        self._dominators()
        self._loops()
        seq = self._goto(0, None, None)
        count = 0
        for b in self.reach:
            if b in self.emitted:
                count += 1
        if count != len(self.reach):
            raise Unstructurable("some blocks were not placed")
        return seq

    def _check_shape(self):
        for i in range(self.n):
            k = self.kinds[i]
            want = {"ret": 0, "jmp": 1, "fall": 1, "cond": 2}[k]
            if len(self.succs[i]) != want:
                raise Unstructurable("block %d: %s with %d successors"
                                     % (i, k, len(self.succs[i])))
            for t in self.succs[i]:
                if t < 0 or t >= self.n:
                    raise Unstructurable("branch to a missing block")

    def _reachable(self):
        seen = {0: True}
        order = []
        stack = [0]
        while stack:
            b = stack.pop()
            order.append(b)
            for t in self.succs[b]:
                if t not in seen:
                    seen[t] = True
                    stack.append(t)
        self.reach = seen
        for b in seen:
            for t in self.succs[b]:
                if b not in self.preds[t]:
                    self.preds[t].append(b)

    def _rpo(self):
        out = []
        seen = {}
        # iterative DFS postorder
        stack = [(0, 0)]
        seen[0] = True
        while stack:
            b, k = stack.pop()
            if k < len(self.succs[b]):
                stack.append((b, k + 1))
                t = self.succs[b][k]
                if t not in seen:
                    seen[t] = True
                    stack.append((t, 0))
            else:
                out.append(b)
        out.reverse()
        return out

    def _dominators(self):
        order = self._rpo()
        self.rpo = order
        index = {}
        for i in range(len(order)):
            index[order[i]] = i
        self.rpo_index = index
        idom = {0: 0}
        changed = True
        while changed:
            changed = False
            for b in order:
                if b == 0:
                    continue
                new = None
                for p in self.preds[b]:
                    if p not in idom:
                        continue
                    if new is None:
                        new = p
                    else:
                        new = self._intersect(idom, new, p)
                if new is not None and idom.get(b) != new:
                    idom[b] = new
                    changed = True
        self.idom = idom

    def _intersect(self, idom, a, b):
        while a != b:
            while self.rpo_index[a] > self.rpo_index[b]:
                a = idom[a]
            while self.rpo_index[b] > self.rpo_index[a]:
                b = idom[b]
        return a

    def dominates(self, a, b):
        while True:
            if a == b:
                return True
            if b == 0:
                return False
            b = self.idom[b]

    def _loops(self):
        # A retreating edge must be a back edge (target dominates source),
        # otherwise the CFG is irreducible.
        bodies = {}
        for u in self.rpo:
            for h in self.succs[u]:
                if self.rpo_index[h] <= self.rpo_index[u]:
                    if not self.dominates(h, u):
                        raise Unstructurable("irreducible control flow")
                    body = bodies.setdefault(h, {h: True})
                    stack = [u]
                    while stack:
                        x = stack.pop()
                        if x in body:
                            continue
                        body[x] = True
                        for p in self.preds[x]:
                            stack.append(p)
        loops = []
        for h in bodies:
            body = bodies[h]
            exits = {}
            for b in body:
                for t in self.succs[b]:
                    if t not in body:
                        exits[t] = True
            if len(exits) > 1:
                joined = self._join_exits(h, body, list(exits))
                if joined is None:
                    raise Unstructurable("loop at block %d has %d different "
                                         "exit targets" % (h, len(exits)))
                exits = {}
                if joined[0] is not None:
                    exits[joined[0]] = True
                for pad in joined[1]:
                    body[pad] = True
            ex = None
            for t in exits:
                ex = t
            lp = _Loop(h, body, ex)
            loops.append(lp)
            self.loop_of_header[h] = lp
        # nesting: bodies must nest or be disjoint
        for a in loops:
            for b in loops:
                if a is b:
                    continue
                common = False
                for x in a.body:
                    if x in b.body:
                        common = True
                        break
                if common:
                    a_in_b = a.header in b.body and len(a.body) < len(b.body)
                    b_in_a = b.header in a.body and len(b.body) < len(a.body)
                    if not a_in_b and not b_in_a:
                        raise Unstructurable("overlapping loops")
        for a in loops:
            best = None
            for b in loops:
                if a is b:
                    continue
                if a.header in b.body and len(b.body) > len(a.body):
                    if best is None or len(b.body) < len(best.body):
                        best = b
            a.parent = best
            if best is not None:
                best.children.append(a)
        self.inner = {}
        for b in self.reach:
            best = None
            for lp in loops:
                if b in lp.body and (best is None
                                     or len(lp.body) < len(best.body)):
                    best = lp
            self.inner[b] = best

    # ------------------------------------------------- the graph around a branch

    def _join_exits(self, h, body, exits):
        """A loop whose exits are several blocks that all lead to one place
        (a `break` pad next to the loop test). Find that place X; the blocks
        on the way to it become part of the loop, ending in a `break`.
        Returns (X or None, pad blocks) or None."""
        out = {}
        stack = list(exits)
        while stack:
            v = stack.pop()
            if v in out or v in body:
                continue
            out[v] = True
            for t in self.succs[v]:
                stack.append(t)
        # Prefer the place the most exits lead to (the code after the loop)
        # over a lone `return` pad that happens to come first.
        closure = {}
        for e in exits:
            seen = {}
            todo = [e]
            while todo:
                v = todo.pop()
                if v in seen or v in body:
                    continue
                seen[v] = True
                for t in self.succs[v]:
                    todo.append(t)
            closure[e] = seen
        score = {}
        for v in out:
            n = 0
            for e in exits:
                if v in closure[e]:
                    n += 1
            score[v] = n
        cands = sorted(out, key=lambda v: (-score[v], self.rpo_index[v]))
        cands.append(None)
        for x in cands:
            pads = {}
            ok = True
            for e in exits:
                if e == x:
                    continue
                todo = [e]
                while todo and ok:
                    v = todo.pop()
                    if v == x or v in pads:
                        continue
                    if v in body or not self.dominates(h, v):
                        ok = False
                        break
                    pads[v] = True
                    for t in self.succs[v]:
                        todo.append(t)
                if not ok:
                    break
            if ok:
                # Nothing outside the loop and its pads may jump into a pad.
                for v in pads:
                    for pr in self.preds[v]:
                        if pr not in body and pr not in pads:
                            ok = False
            if ok:
                return x, pads
        return None

    def _child_loop_at(self, loop, s):
        """The loop directly inside `loop` (or at top level when loop is
        None) that contains block s, or None if s belongs to `loop` itself."""
        lp = self.inner[s]
        while lp is not None and lp.parent is not loop:
            lp = lp.parent
        return lp

    def _view(self, loop):
        """Successor function of the graph seen from inside `loop`: edges
        that leave the loop or go back to its header are dropped (they are
        `break` / `continue`), and each inner loop is contracted to its
        header, whose only successor is the loop's exit."""
        cache = {}

        def succ(b):
            if b in cache:
                return cache[b]
            out = []
            lpb = self._child_loop_at(loop, b)
            if lpb is not None and lpb.header == b:
                if lpb.exit is not None and self._in(loop, lpb.exit) and \
                        lpb.exit != (loop.header if loop else -1):
                    out.append(self._enter(loop, lpb.exit))
            else:
                for t in self.succs[b]:
                    if loop is not None:
                        if t == loop.header or t not in loop.body:
                            continue
                    out.append(self._enter(loop, t))
            cache[b] = out
            return out
        return succ

    def _in(self, loop, b):
        return loop is None or b in loop.body

    def _enter(self, loop, t):
        """Node standing for block t in the contracted graph."""
        lp = self._child_loop_at(loop, t)
        if lp is not None:
            return lp.header
        return t

    def _postdoms(self, loop, start):
        """ipdom-style analysis restricted to what is reachable from
        `start` in the contracted view. Returns (pdom sets, reach)."""
        succ = self._view(loop)
        nodes = {}
        stack = [start]
        while stack:
            b = stack.pop()
            if b in nodes:
                continue
            nodes[b] = True
            for t in succ(b):
                stack.append(t)
        # Paths that leave on their own -- a `return`, a `break`, a
        # `continue` -- need not rejoin the others, so a dead end that is
        # reached from only one place is pruned (repeatedly: pruning can
        # turn its predecessor into a dead end). A dead end reached from two
        # or more places is a real join and stays.
        removed = {}
        changed = True
        while changed:
            changed = False
            count = {}
            for b in nodes:
                if b in removed:
                    continue
                for t in succ(b):
                    if t not in removed:
                        count[t] = count.get(t, 0) + 1
            for b in nodes:
                if b in removed or b == start:
                    continue
                live = [t for t in succ(b) if t not in removed]
                if not live and count.get(b, 0) < 2:
                    removed[b] = True
                    changed = True
        alive = {}
        for b in nodes:
            if b not in removed:
                alive[b] = True

        def live_succ(b):
            return [t for t in succ(b) if t not in removed]

        pd = {}
        for b in alive:
            pd[b] = dict(alive)
        changed = True
        while changed:
            changed = False
            for b in alive:
                ss = live_succ(b)
                if not ss:
                    new = {b: True}
                else:
                    new = None
                    for t in ss:
                        if new is None:
                            new = dict(pd[t])
                        else:
                            for x in list(new):
                                if x not in pd[t]:
                                    del new[x]
                    new[b] = True
                if len(new) != len(pd[b]):
                    pd[b] = new
                    changed = True
        return pd, alive, succ

    def _merge_point(self, b, loop):
        """The immediate post-dominator of the branch at b in this loop's
        view, or None when the arms never rejoin."""
        pd, _nodes, _succ = self._postdoms(loop, b)
        strict = []
        for x in pd[b]:
            if x != b:
                strict.append(x)
        best = None
        for x in strict:
            if best is None or len(pd[x]) > len(pd[best]):
                best = x
        return best

    def _closed(self, s, loop, stop):
        """True when every block reachable from s (in the contracted view)
        is dominated by s and none of them is the enclosing stop block, so s
        can be emitted as a self-contained arm."""
        if stop is not None and s == stop:
            return False
        if loop is not None and (s == loop.header or s not in loop.body):
            return True
        succ = self._view(loop)
        seen = {}
        stack = [self._enter(loop, s)]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen[x] = True
            if not self.dominates(s, x):
                return False
            if stop is not None and x == stop:
                return False
            if loop is not None and x == loop.header:
                continue
            for t in succ(x):
                stack.append(t)
        return True

    # ----------------------------------------------------------- emission

    def _goto(self, z, loop, stop):
        """Items that transfer control to block z from the current spot."""
        if loop is not None:
            if z == loop.header:
                return [("br", "continue")]
            if z not in loop.body:
                if z != loop.exit:
                    raise Unstructurable("branch out of nested loops")
                return [("br", "break")]
        if stop is not None and z == stop:
            return [("br", "merge")]
        if z in self.emitted:
            if self._copyable(z):
                return self._copy(z, loop, stop)
            raise Unstructurable("block %d is reached from two places "
                                 "that do not join" % z)
        inner = self.inner[z]
        if inner is not loop:
            # z is inside a loop deeper than ours: it must be that loop's
            # header (loops are entered only through the header).
            lp = self._child_loop_at(loop, z)
            if lp is None or lp.header != z:
                raise Unstructurable("jump into the middle of a loop")
            return self._loop(lp, loop, stop)
        return self._block(z, loop, stop)

    def _copyable(self, z):
        if self.sizes is None or self.depth >= DUP_DEPTH:
            return False
        if self.kinds[z] not in ("jmp", "fall", "ret"):
            return False
        return self.sizes[z] <= DUP_LIMIT and z not in self.loop_of_header

    def _copy(self, z, loop, stop):
        items = [("block", z)]
        if self.kinds[z] == "ret":
            items.append(("ret", z))
            return items
        self.depth += 1
        try:
            return items + self._goto(self.succs[z][0], loop, stop)
        finally:
            self.depth -= 1

    def _loop(self, lp, outer, stop):
        body = self._block(lp.header, lp, None)
        items = [("loop", lp.header, body, lp.exit is not None)]
        if lp.exit is not None:
            items += self._goto(lp.exit, outer, stop)
        return items

    def _block(self, b, loop, stop):
        self.emitted[b] = True
        k = self.kinds[b]
        items = [("block", b)]
        if k == "ret":
            items.append(("ret", b))
            return items
        if k == "jmp" or k == "fall":
            return items + self._goto(self.succs[b][0], loop, stop)
        t, f = self.succs[b]
        if t == f:
            raise Unstructurable("conditional branch with equal targets")
        p = self._merge_point(b, loop)
        if p is not None:
            then = self._goto(t, loop, p)
            els = self._goto(f, loop, p)
            items.append(("if", b, then, els, True))
            return items + self._goto(p, loop, stop)
        ct = self._closed(t, loop, stop)
        cf = self._closed(f, loop, stop)
        if ct and cf:
            then = self._goto(t, loop, None)
            els = self._goto(f, loop, None)
            items.append(("if", b, then, els, False))
            return items
        if ct:
            then = self._goto(t, loop, None)
            items.append(("if", b, then, [], True))
            return items + self._goto(f, loop, stop)
        if cf:
            els = self._goto(f, loop, None)
            items.append(("if", b, [], els, True))
            return items + self._goto(t, loop, stop)
        raise Unstructurable("branch at block %d has no merge point" % b)
