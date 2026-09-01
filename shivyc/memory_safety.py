"""Memory-safety analysis for ShivyCX (use-after-free / double-free / auto-free).

C's manual `malloc`/`free` is the classic source of use-after-free and
double-free bugs. Because ShivyCX sees the whole call graph, a Python pass can
track every allocation, pointer copy (alias) and free across the program and:

  * flag a dereference of a pointer whose allocation has been freed
    (use-after-free), including through aliases,
  * flag freeing an allocation twice (double-free),
  * and -- the more ambitious capability -- identify allocations that are
    provably local with no escaping reference, which the compiler can free
    automatically so the programmer may omit `free` entirely.

This recovers much of Rust's ownership safety for ordinary, unannotated C,
driven by whole-program reachability rather than annotations.

The analysis works on ShivyCX's IL (the same `Call.direct_name` / `Set` /
`ReadAt` / `SetAt` commands the other whole-program passes use), so it sees
aliasing as it actually flows through the generated code.
"""

import sys
import shivyc.il_cmds.control as control_cmds
import shivyc.il_cmds.math as math_cmds
import shivyc.il_cmds.value as value_cmds

# Functions that return a fresh heap allocation owned by the caller.
ALLOCATORS = {
    "malloc", "calloc", "realloc", "aligned_alloc", "strdup", "strndup",
    "kmalloc", "kmalloc_aligned",
}
# Functions that free their (first) pointer argument.
DEALLOCATORS = {"free", "kfree"}


# ---------------------------------------------------------------------------
# Whole-program IL loader (mirrors callgraph.build_program_graph, but keeps the
# per-function command lists and the ILValue->name map for diagnostics).
# ---------------------------------------------------------------------------
class Program:
    def __init__(self):
        self.functions = {}     # name -> [il commands]
        self.names = {}         # ILValue -> source identifier
        self.edges = {}         # name -> set(callee names)
        self.defined = set()    # names with a body in this program
        self.globals = set()    # ILValues with static storage or linkage


def _transpile_py_for_analysis(file):
    """Transpile an rpython `.py` input to a C file the memory analyzer can
    parse, and return its path. The runtime header is written alongside so the
    generated `#include "shivyc_rt.h"` (and the `obj` types it defines) resolve.
    Returns the .c path, or None on failure."""
    import os
    import sys
    import tempfile
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tools = os.path.join(repo, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        import py2c
    except Exception:
        return None
    d = tempfile.mkdtemp(prefix="shivyc_mem_")
    try:
        cpath, err = py2c.transpile_file(file, d)
        py2c.write_runtime(d)        # so #include "shivyc_rt.h" resolves
    except Exception:
        return None
    if err or not cpath:
        return None
    return cpath


def load_program(files, args):
    import shivyc.lexer as lexer
    import shivyc.preproc as preproc
    import shivyc.weak_alias as weak_alias
    import shivyc.cache as cache
    import shivyc.stackless as stackless
    import shivyc.main as main_mod
    import shivyc.extensions as extensions
    from shivyc.errors import error_collector
    from shivyc.parser.parser import parse
    from shivyc.tree.general_nodes import Root
    from shivyc.il_gen import ILCode, SymbolTable, Context

    prog = Program()
    ok = True
    for file in files:
        if file.endswith(".py"):
            src_path = _transpile_py_for_analysis(file)
            if src_path is None:
                ok = False
                continue
        elif file.endswith(".c"):
            src_path = file
        else:
            continue
        try:
            code = open(src_path).read()
        except OSError:
            ok = False
            continue
        try:
            code, _ = extensions.preprocess_extensions(code)
        except extensions.ExtensionError:
            ok = False
            continue

        error_collector.clear()
        tokens = preproc.process(lexer.tokenize(code, src_path), src_path)
        tokens, _ = weak_alias.extract_aliases(tokens)
        tokens = main_mod._concat_adjacent_strings(tokens)
        key = cache.token_key(tokens)
        if sys.implementation.name != 'shivyc':
            ast = cache.load_ast(key)
            if ast is None:
                ast = parse(tokens)
                if ast is not None and error_collector.ok():
                    cache.store_ast(key, ast)
        else:
            # Host-only AST cache (pickles); self-host parses directly so `ast`
            # stays typed and `ast.make_il(...)` is a vtable dispatch.
            ast = parse(tokens)
        if ast is None:
            ok = False
            continue
        il, st = ILCode(), SymbolTable()
        try:
            ast.make_il(il, st, Context())
        except Exception:
            ok = False
            continue
        if not error_collector.ok():
            ok = False

        for fn in list(il.commands):
            il.commands[fn] = stackless._apply_direct_calls(il.commands[fn], st)
            prog.functions[fn] = il.commands[fn]
            prog.defined.add(fn)
            edges = prog.edges.setdefault(fn, set())
            for cmd in il.commands[fn]:
                if isinstance(cmd, control_cmds.Call) and cmd.direct_name:
                    edges.add(cmd.direct_name)
        for v, n in st.names.items():
            prog.names[v] = n
        for v, sto in getattr(st, "storage", {}).items():
            if sto == st.STATIC:
                prog.globals.add(v)
        for v in getattr(st, "linkage_type", {}):
            prog.globals.add(v)
    return prog, ok


# ---------------------------------------------------------------------------
# Control-flow graph over a flat IL command list.
# ---------------------------------------------------------------------------
class CFG:
    """Basic-block CFG built from one function's IL command list."""

    def __init__(self, cmds):
        self.cmds = cmds
        self.labels = {}        # label -> command index
        for i, c in enumerate(cmds):
            if isinstance(c, control_cmds.Label):
                self.labels[c.label] = i
        self.leaders = self._find_leaders()
        self.blocks, self.block_of = self._make_blocks()
        self.succ = self._make_edges()

    def _is_branch(self, c):
        return isinstance(c, (control_cmds.Jump, control_cmds._GeneralJump))

    def _find_leaders(self):
        leaders = {0} if self.cmds else set()
        for i, c in enumerate(self.cmds):
            if isinstance(c, control_cmds.Label):
                leaders.add(i)
            if self._is_branch(c) or isinstance(c, control_cmds.Return):
                if i + 1 < len(self.cmds):
                    leaders.add(i + 1)
            if isinstance(c, control_cmds._GeneralJump):
                leaders.add(self.labels.get(c.label, 0))
            if isinstance(c, control_cmds.Jump):
                leaders.add(self.labels.get(c.label, 0))
        return leaders

    def _make_blocks(self):
        starts = sorted(self.leaders)
        blocks = []         # list of (start, end_exclusive)
        block_of = {}
        for k, s in enumerate(starts):
            e = starts[k + 1] if k + 1 < len(starts) else len(self.cmds)
            bid = len(blocks)
            blocks.append((s, e))
            for i in range(s, e):
                block_of[i] = bid
        return blocks, block_of

    def _make_edges(self):
        succ = {b: set() for b in range(len(self.blocks))}
        for b, (s, e) in enumerate(self.blocks):
            if e == s:
                continue
            last = self.cmds[e - 1]
            if isinstance(last, control_cmds.Return):
                continue
            if isinstance(last, control_cmds.Jump):
                tgt = self.labels.get(last.label)
                if tgt is not None:
                    succ[b].add(self.block_of[tgt])
                continue
            if isinstance(last, control_cmds._GeneralJump):
                tgt = self.labels.get(last.label)
                if tgt is not None:
                    succ[b].add(self.block_of[tgt])
                if e < len(self.cmds):           # fallthrough
                    succ[b].add(self.block_of[e])
                continue
            if e < len(self.cmds):               # ordinary fallthrough
                succ[b].add(self.block_of[e])
        return succ


# ---------------------------------------------------------------------------
# Abstract state: may-points-to + per-allocation free/escape status.
# ---------------------------------------------------------------------------
# freed status lattice: 'a' (allocated) < 'mf' (maybe freed) < 'f' (freed)
def _merge_freed(x, y):
    if x == y:
        return x
    return "mf"


class State:
    __slots__ = ("pt", "freed", "escaped")

    def __init__(self, pt=None, freed=None, escaped=None):
        self.pt = dict(pt or {})            # ILValue -> frozenset(alloc_id)
        self.freed = dict(freed or {})      # alloc_id -> 'a'|'mf'|'f'
        self.escaped = set(escaped or ())   # alloc_id

    def copy_state(self):
        return State(self.pt, self.freed, self.escaped)

    def __eq__(self, o):
        return (self.pt == o.pt and self.freed == o.freed
                and self.escaped == o.escaped)

    def merge(self, o):
        out = self.copy_state()
        for v, a in o.pt.items():
            out.pt[v] = out.pt.get(v, frozenset()) | a
        for al, s in o.freed.items():
            out.freed[al] = _merge_freed(out.freed.get(al, s), s)
        out.escaped |= o.escaped
        return out


# ---------------------------------------------------------------------------
# Per-function summary (for interprocedural propagation).
# ---------------------------------------------------------------------------
class Summary:
    def __init__(self):
        self.frees_params = set()     # param indices freed by the callee
        self.escapes_params = set()   # param indices that escape (stored/returned/passed on)
        self.derefs_params = set()    # param indices the callee dereferences
        self.returns_alloc = False    # returns a fresh/owned allocation


class Diagnostic:
    def __init__(self, func, kind, alloc, detail="", r=None, alloc_r=None,
                 free_r=None):
        self.func = func
        self.kind = kind              # 'use-after-free' | 'double-free' | 'leak'
        self.alloc = alloc
        self.detail = detail
        # Where the offending access is, where the object was allocated, and
        # where it was freed. Before IL commands carried source ranges this
        # pass could only say "in <function>"; all three are now recoverable,
        # and the allocation site in particular is usually where the fix goes.
        self.r = r
        self.alloc_r = alloc_r
        self.free_r = free_r

    def __repr__(self):
        return f"<{self.kind} in {self.func}: {self.detail}>"


def _loc(r):
    """Render a range as `file:line`, or "" when there is none."""
    if r is None or getattr(r, "start", None) is None:
        return ""
    return "%s:%d" % (r.start.file, r.start.line)


def _alloc_range(prog, al):
    """Range of the command that created allocation `al`.

    Allocation ids are (function, command index) pairs, so the site is
    recoverable from the program itself -- the analysis does not have to carry
    it along in its dataflow state.
    """
    if not isinstance(al, tuple) or len(al) < 2:
        return None
    cmds = prog.functions.get(al[0])
    if not cmds or not isinstance(al[1], int) or al[1] >= len(cmds):
        return None
    return cmds[al[1]].r


def _param_map(cmds):
    """arg_num -> ILValue for the function's parameters (from LoadArg)."""
    out = {}
    for c in cmds:
        if isinstance(c, value_cmds.LoadArg):
            out[c.arg_num] = c.output
    return out


def _alloc_name(alloc):
    """Readable label for an allocation id."""
    if isinstance(alloc, tuple) and alloc and alloc[0] == "param":
        return "parameter %s" % (alloc[2],)
    return "allocation"


def _use_check(ctx, fn, freed, idx, pt, diags, addr: "ILValue", kind, r=None):
    """Record a use-after-free if `addr` still points at a freed allocation.

    Module-level (not nested): py2c name-infers bare `addr` as `int`, but
    callers pass an ILValue; annotate so `make bootstrap` stays green.
    """
    for al in pt.get(addr, ()):
        ctx.derefed.add(al)
        if freed.get(al) in ("f", "mf") and ctx.record:
            key = (idx, kind, al)
            if key not in ctx.seen_diag:
                ctx.seen_diag.add(key)
                diags.append(Diagnostic(
                    fn, "use-after-free", al, kind, r,
                    _alloc_range(ctx.prog, al), ctx.freed_at.get(al)))


def _note_live_access(ctx, freed, idx, pt, addr: "ILValue"):
    """Record that command `idx` dereferences a provably live allocation.

    Only a *single* target counts. If the pointer may refer to either of two
    allocations, their sizes may differ, and an offset in bounds for one can be
    out of bounds for the other -- there is nothing to prove.

    State "a" means allocated and not since freed. Anything else ("f", "mf",
    or absent) leaves the access unproven, and it keeps its runtime check.
    """
    als = pt.get(addr)
    if not als or len(als) != 1:
        ctx.live_access.pop(idx, None)
        return
    for al in als:
        if freed.get(al) == "a":
            ctx.live_access[idx] = al
        else:
            ctx.live_access.pop(idx, None)


class _Ctx:
    """Per-function analysis context shared across the dataflow."""
    def __init__(self, fn, record, prog=None):
        self.fn = fn
        self.record = record
        self.prog = prog
        self.freed_at = {}        # alloc_id -> range of the free that killed it
        # cmd index -> the single allocation a dereference provably targets,
        # when that allocation is live at this point. Consumed by --mem-safe's
        # instrumentation pass to drop checks it does not need. Written on
        # every visit and read only after the fixpoint settles, so what
        # survives is the converged answer rather than some intermediate one.
        self.live_access = {}
        self.seen_diag = set()
        self.owned_local = {}     # alloc_id -> live ILValue holding it
        self.escaped_ever = set()
        self.freed_ever = set()
        self.derefed = set()      # alloc_ids dereferenced anywhere
        self.returned = False     # a heap alloc reached a Return
        self.param_seed = {}      # ILValue(param) -> frozenset(alloc) for summaries


class Analyzer:
    prog: Program

    def __init__(self, prog):
        self.prog = prog
        self.summaries = {}           # func -> Summary
        self.diags = []
        self.autofree = {}            # func -> list of (cmd_index, ILValue, alloc)
        self.live_access = {}         # func -> {cmd index: allocation id}

    # -- ordering: callees before callers (reverse topological-ish) --------
    def _order(self):
        edges = self.prog.edges
        order, seen, temp = [], set(), set()

        def visit(n):
            if n in seen or n not in self.prog.functions:
                return
            if n in temp:                 # recursion: break cycle
                return
            temp.add(n)
            for m in edges.get(n, ()):    # visit callees first
                visit(m)
            temp.discard(n)
            seen.add(n)
            order.append(n)
        for fn in self.prog.functions:
            visit(fn)
        return order

    def run(self):
        order = self._order()
        for fn in order:                  # summaries: callees first
            self.summaries[fn] = self._summarize(fn)
        for fn in self.prog.functions:    # full check with summaries available
            self._check(fn)
        return self.diags, self.autofree

    # -- summary computation (seed params, see what escapes/frees) ---------
    def _summarize(self, fn):
        cmds = self.prog.functions[fn]
        params = _param_map(cmds)
        seeds = {}
        ctx = _Ctx(fn, False, self.prog)
        for idx, val in params.items():
            ct = getattr(val, "ctype", None)
            if ct is not None and ct.is_pointer():
                al = ("param", fn, idx)
                ctx.param_seed[val] = frozenset({al})
                seeds[al] = idx
        exits = self._dataflow(fn, cmds, State(), ctx)

        s = Summary()
        for st in exits:
            for al, idx in seeds.items():
                if st.freed.get(al) == "f":
                    s.frees_params.add(idx)
                if al in st.escaped:
                    s.escapes_params.add(idx)
        for al, idx in seeds.items():
            if al in ctx.derefed:
                s.derefs_params.add(idx)
        s.returns_alloc = ctx.returned
        return s

    # -- full check (emit diagnostics + collect auto-free candidates) ------
    def _check(self, fn):
        cmds = self.prog.functions[fn]
        ctx = _Ctx(fn, True, self.prog)
        self._dataflow(fn, cmds, State(), ctx)
        self.live_access[fn] = ctx.live_access
        for al, val in ctx.owned_local.items():
            if al in ctx.escaped_ever or al in ctx.freed_ever:
                continue
            self.autofree.setdefault(fn, []).append((al, val))

    # -- the dataflow engine ----------------------------------------------
    def _dataflow(self, fn, cmds, init, ctx):
        cfg = CFG(cmds)
        n = len(cfg.blocks)
        if n == 0:
            return [init]
        in_state = [None] * n
        out_state = [None] * n
        in_state[0] = init
        worklist = [0]
        while worklist:
            b = worklist.pop()
            s = in_state[b].copy_state()
            start, end = cfg.blocks[b]
            for i in range(start, end):
                self._transfer(cmds[i], i, s, ctx)
            out_state[b] = s
            for succ in cfg.succ[b]:
                merged = s if in_state[succ] is None else in_state[succ].merge(s)
                if in_state[succ] is None or merged != in_state[succ]:
                    in_state[succ] = merged
                    if succ not in worklist:
                        worklist.append(succ)

        exits = []
        for b in range(n):
            if out_state[b] is not None and not cfg.succ[b]:
                exits.append(out_state[b])
        return exits or [out_state[0] or init]

    def _transfer(self, c, idx, s, ctx):
        fn = ctx.fn
        pt, freed, escaped = s.pt, s.freed, s.escaped

        def mark_escape(al):
            escaped.add(al)
            ctx.escaped_ever.add(al)

        def mark_free(al):
            freed[al] = "f"
            ctx.freed_ever.add(al)
            # First free wins: for a double free the interesting site is the
            # one that made the pointer dangle, not the one that tripped over
            # it afterwards.
            if al not in ctx.freed_at:
                ctx.freed_at[al] = c.r

        if isinstance(c, control_cmds.Call):
            name = c.direct_name
            args = list(c.args)
            if name in ALLOCATORS:
                if c.ret is not None:
                    al = (fn, idx)
                    pt[c.ret] = frozenset({al})
                    freed[al] = "a"
                    ctx.owned_local[al] = c.ret
                return
            if name in DEALLOCATORS and args:
                for al in pt.get(args[0], ()):
                    if freed.get(al) in ("f", "mf") and ctx.record:
                        key = (idx, "double-free", al)
                        if key not in ctx.seen_diag:
                            ctx.seen_diag.add(key)
                            self.diags.append(Diagnostic(
                                fn, "double-free", al,
                                "free of an %s that was already freed" % (
                                    _alloc_name(al),), c.r,
                                _alloc_range(self.prog, al),
                                ctx.freed_at.get(al)))
                    mark_free(al)
                return
            summ = self.summaries.get(name)
            if summ is not None and name in self.prog.defined:
                for i, a in enumerate(args):
                    if i in summ.derefs_params:        # callee dereferences arg i
                        _use_check(
                            ctx, fn, freed, idx, pt, self.diags, a,
                            "passes a freed pointer to %s(), which "
                            "dereferences it" % (name,), c.r)
                    if i in summ.frees_params:
                        for al in pt.get(a, ()):
                            if freed.get(al) in ("f", "mf") and ctx.record:
                                key = (idx, "double-free", al)
                                if key not in ctx.seen_diag:
                                    ctx.seen_diag.add(key)
                                    self.diags.append(Diagnostic(
                                        fn, "double-free", al,
                                        "free of an allocation already freed "
                                        "(via %s)" % (name,), c.r,
                                        _alloc_range(self.prog, al),
                                        ctx.freed_at.get(al)))
                            mark_free(al)
                    if i in summ.escapes_params:
                        for al in pt.get(a, ()):
                            mark_escape(al)
                if c.ret is not None:
                    if summ.returns_alloc:
                        al = (fn, idx, "ret")
                        pt[c.ret] = frozenset({al})
                        freed[al] = "a"
                        ctx.owned_local[al] = c.ret
                    else:
                        pt[c.ret] = frozenset()
                return
            # unknown / external call: pointer args may be used and they escape
            for a in args:
                _use_check(ctx, fn, freed, idx, pt, self.diags, a,
                           "passes a freed pointer to a function", c.r)
                for al in pt.get(a, ()):
                    mark_escape(al)
            if c.ret is not None:
                pt[c.ret] = frozenset()
            return

        if isinstance(c, value_cmds.Set):
            src = pt.get(c.arg)
            if src:
                pt[c.output] = src
                if c.output in self.prog.globals:
                    for al in src:        # stored into a global -> escapes
                        mark_escape(al)
                else:
                    # live holder is now `output` (move/alias); all in-function
                    # holders die together at scope end, so this alone does not
                    # disqualify auto-free.
                    for al in src:
                        if al in ctx.owned_local:
                            ctx.owned_local[al] = c.output
            else:
                pt[c.output] = frozenset()
            return

        if isinstance(c, math_cmds.Add) or isinstance(c, math_cmds.Subtr):
            # Pointer arithmetic stays inside the object it started in -- that
            # is what C guarantees, and computing `p + k` does not make the
            # result point somewhere else. Carrying the points-to set across
            # it is what lets this pass see through `p->field` and `a[i]`,
            # which both lower to an Add before the dereference.
            #
            # Without this the analysis lost the allocation at the first
            # offset computation, so a use-after-free written as `p->key`
            # went unreported while the same bug written as `*p` was caught.
            src = pt.get(c.arg1) or frozenset()
            if isinstance(c, math_cmds.Add) and not src:
                # `k + p` is the same expression as `p + k`. Subtraction is
                # not symmetric, so only Add looks at the right operand.
                src = pt.get(c.arg2) or frozenset()
            pt[c.output] = src
            return

        if isinstance(c, value_cmds.ReadAt):
            _use_check(
                ctx, fn, freed, idx, pt, self.diags, c.addr,
                "dereferences a pointer after its allocation was freed", c.r)
            _note_live_access(ctx, freed, idx, pt, c.addr)
            pt[c.output] = frozenset()
            return

        if isinstance(c, value_cmds.SetAt):
            _use_check(
                ctx, fn, freed, idx, pt, self.diags, c.addr,
                "dereferences a pointer after its allocation was freed", c.r)
            _note_live_access(ctx, freed, idx, pt, c.addr)
            for al in pt.get(c.val, ()):
                mark_escape(al)
            return

        if isinstance(c, control_cmds.Return):
            if c.arg is not None:
                for al in pt.get(c.arg, ()):
                    mark_escape(al)
                    ctx.returned = True
            return

        if isinstance(c, value_cmds.LoadArg):
            seed = ctx.param_seed.get(c.output)
            if seed:
                pt[c.output] = seed
                for al in seed:
                    freed.setdefault(al, "a")
            else:
                pt[c.output] = frozenset()
            return

        for out in c.outputs():
            pt[out] = frozenset()


# ---------------------------------------------------------------------------
# Driver + reporting
# ---------------------------------------------------------------------------
def _passthrough_params(prog):
    """func -> set of parameter indices it returns unchanged (an identity-like
    'launderer'). Used to follow a stack address that is returned indirectly
    through a helper -- the obfuscation gcc's -Wreturn-local-addr loses."""
    res = {}
    for fn, cmds in prog.functions.items():
        defmap = {}
        for c in cmds:
            for o in c.outputs():
                defmap[o] = c
        pmap = _param_map(cmds)
        inv = {v: k for k, v in pmap.items()}

        def origin_val(v):
            d = defmap.get(v)
            while isinstance(d, value_cmds.Set):
                v = d.arg
                d = defmap.get(v)
            return v

        rps = set()
        for c in cmds:
            if isinstance(c, control_cmds.Return) and c.arg is not None:
                ov = origin_val(c.arg)
                if ov in inv:
                    rps.add(inv[ov])
        res[fn] = rps
    return res


def _dangling_stack_diags(prog):
    """Whole-program check: a function that returns the address of one of its
    own locals (`return &x;`), directly or laundered through a passthrough
    helper. After the call the stack frame is gone, so the caller holds a
    dangling pointer. gcc's -Wreturn-local-addr catches the direct form but
    loses it once the address is returned indirectly through another function;
    because we see the whole call graph we can follow it."""
    passthrough = _passthrough_params(prog)
    diags = []
    for fn, cmds in prog.functions.items():
        defmap = {}
        for c in cmds:
            for o in c.outputs():
                defmap[o] = c

        def origin(v):
            d = defmap.get(v)
            while isinstance(d, value_cmds.Set):
                v = d.arg
                d = defmap.get(v)
            return d

        def is_local_addr(v):
            d = origin(v)
            return (isinstance(d, value_cmds.AddrOf)
                    and d.var not in prog.globals), d

        for c in cmds:
            if not (isinstance(c, control_cmds.Return) and c.arg is not None):
                continue
            d = origin(c.arg)
            if isinstance(d, value_cmds.AddrOf) and d.var not in prog.globals:
                nm = prog.names.get(d.var, "a local")
                diags.append(Diagnostic(
                    fn, "dangling-stack-pointer", None,
                    "returns the address of local '%s'; that stack memory is "
                    "reclaimed when the function returns" % nm))
            elif isinstance(d, control_cmds.Call) and \
                    getattr(d, "direct_name", None) in passthrough:
                args = getattr(d, "args", [])
                for k in passthrough[d.direct_name]:
                    if k < len(args):
                        bad, ad = is_local_addr(args[k])
                        if bad:
                            nm = prog.names.get(ad.var, "a local")
                            diags.append(Diagnostic(
                                fn, "dangling-stack-pointer", None,
                                "returns the address of local '%s' laundered "
                                "through %s(); the stack frame is gone on "
                                "return" % (nm, d.direct_name)))
    return diags


def analyze_program(files, args):
    prog, ok = load_program(files, args)
    diags, autofree = Analyzer(prog).run()
    diags = list(diags) + _dangling_stack_diags(prog)
    return prog, diags, autofree, ok


def format_report(diags, autofree, prog=None):
    out = []
    if diags:
        out.append("memory-safety issues:")
        for d in diags:
            # Lead with the location when there is one, the way every other
            # diagnostic in the compiler does, and keep the function name --
            # the two answer different questions for a whole-program pass that
            # can flag a use in one function caused by a free in another.
            where = _loc(d.r)
            if where:
                out.append(f"  {where}: [{d.kind}] in {d.func}: {d.detail}")
            else:
                out.append(f"  [{d.kind}] in {d.func}: {d.detail}")
            # The allocation and the free are where the fix usually goes; the
            # use is only where it surfaced.
            aloc = _loc(d.alloc_r)
            floc = _loc(d.free_r)
            if aloc:
                out.append(f"      allocated at {aloc}")
            if floc:
                out.append(f"      freed at {floc}")
    else:
        out.append("memory-safety: no use-after-free or double-free found")
    leaks = sum(len(v) for v in autofree.values())
    if leaks:
        out.append("")
        out.append(f"auto-free candidates ({leaks}): local allocations with no "
                   "escaping reference, safe to free automatically:")
        for fn, items in sorted(autofree.items()):
            out.append(f"  in {fn}: {len(items)} allocation(s) the compiler can "
                       "free at function exit")
            # Naming the allocation lets the programmer check the call rather
            # than take the analysis on faith -- worth more than the count,
            # which on its own says only that *something* was found.
            if prog is not None:
                for entry in items:
                    aloc = _loc(_alloc_range(prog, entry[0]))
                    if aloc:
                        out.append(f"      allocated at {aloc}")
    return "\n".join(out)


def run(files, args):
    """Entry point for --check-memory: analyze and print a report."""
    prog, diags, autofree, ok = analyze_program(files, args)
    print(format_report(diags, autofree, prog))
    # nonzero exit if real bugs were found
    return 1 if diags else 0


# ---------------------------------------------------------------------------
# Auto-free insertion (the "compiler inserts free for you" capability).
# ---------------------------------------------------------------------------
def _program_from_il(il_code, symbol_table):
    """Build a Program view over an already-generated TU (no file reload)."""
    import shivyc.stackless as stackless
    prog = Program()
    for fn in list(il_code.commands):
        cmds = stackless._apply_direct_calls(il_code.commands[fn], symbol_table)
        il_code.commands[fn] = cmds          # keep direct_name on the real list
        prog.functions[fn] = cmds
        prog.defined.add(fn)
        edges = prog.edges.setdefault(fn, set())
        for c in cmds:
            if isinstance(c, control_cmds.Call) and c.direct_name:
                edges.add(c.direct_name)
    for v, n in symbol_table.names.items():
        prog.names[v] = n
    for v, sto in getattr(symbol_table, "storage", {}).items():
        if sto == symbol_table.STATIC:
            prog.globals.add(v)
    for v in getattr(symbol_table, "linkage_type", {}):
        prog.globals.add(v)
    return prog


def _find_free_template(il_code):
    """Return a (func ILValue, name) usable to build a `free` call.

    Prefer an existing free/deallocator call (reuse its target verbatim).
    Otherwise synthesize a `free` function value with the libc signature
    `void free(void*)`; `free` resolves at link time. This lets the compiler
    insert frees even when the program never frees anything itself (the common
    case the whole-program analysis is meant to handle).
    """
    for cmds in il_code.commands.values():
        for c in cmds:
            if (isinstance(c, control_cmds.Call)
                    and c.direct_name in DEALLOCATORS):
                return c.func, c.direct_name
    # None present: synthesize `void free(void*)`.
    from shivyc.ctypes import FunctionCType, PointerCType, void
    from shivyc.il_gen import ILValue
    free_ftype = FunctionCType([PointerCType(void)], void, False)
    return ILValue(PointerCType(free_ftype)), "free"


def insert_auto_frees(il_code, symbol_table, args=None):
    """Insert a free() for each provably-local, non-escaping, never-freed
    allocation, right before every Return of the function that owns it.

    Returns the number of frees inserted. Requires a `free` call to already
    exist somewhere in the TU (its target is reused); otherwise reports 0 and
    leaves the program unchanged.
    """
    prog = _program_from_il(il_code, symbol_table)
    _, autofree = Analyzer(prog).run()
    if not autofree:
        return 0
    func_val, free_name = _find_free_template(il_code)
    if func_val is None:
        return 0

    inserted = 0
    for fn, items in autofree.items():
        cmds = il_code.commands[fn]
        holders = [val for (_al, val) in items]
        # insert before each Return (and at the very end if it falls through)
        new = []
        for c in cmds:
            if isinstance(c, control_cmds.Return):
                for h in holders:
                    call = control_cmds.Call(func_val, [h], None)
                    call.direct_name = free_name
                    new.append(call)
                    inserted += 1
            new.append(c)
        if not new or not isinstance(new[-1], control_cmds.Return):
            for h in holders:
                call = control_cmds.Call(func_val, [h], None)
                call.direct_name = free_name
                new.append(call)
                inserted += 1
        il_code.commands[fn] = new
    return inserted
