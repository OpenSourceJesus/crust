#!/usr/bin/env python3
"""minipy_live_fraction -- how much of `st.heap` is garbage at any moment?

MINIPY_MEMORY.md argues that minipy's scaling wall is `st.heap` growing
without bound, and that a precise mark-sweep over it would turn peak memory
from *total allocations* into *live set*. That argument is only worth acting
on if the live set is actually small, so this measures it before anything is
built.

Method: run a program on the reference VM under CPython. At intervals,
stop and trace from the roots, counting how many `Cont` slots are reachable
against how many exist.

THE ROOTS ARE NOT ALL IN `st`. `st.glob`, `st.exc_val` and the method caches
are, and `st.regpool` holds *retired* register arrays. But an active frame's
`regs` is a plain Python local in the interpreter's exec function, and calls
nest by recursion -- so the registers of every frame currently executing
live on the interpreter's own call stack. This walks `sys._getframe` to find
them.

That is a fact about the interpreter, not about this script: a real
collector cannot walk a C stack this way, so it would need `st` to carry an
explicit stack of active frames (pushed and popped alongside `regpool`,
which already exists for the retired ones). That is a small change and a
prerequisite for the collector -- recorded here because measuring is what
surfaced it.

    python3 tools/minipy_live_fraction.py            # the built-in programs
    python3 tools/minipy_live_fraction.py FILE.py    # one of your own
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "rpy_lib"))
sys.setrecursionlimit(200000)

from minipy import compiler, interp                   # noqa: E402
import rpy                                            # noqa: E402


# Programs chosen to span the shapes that matter: garbage that dies
# immediately, garbage that accumulates in a live container, and a workload
# with almost no containers at all. If the live fraction were uniformly high
# the collector would not be worth building, so the point is to find a case
# where it is.
PROGRAMS = {
    "transient-list": """
total = 0
i = 0
while i < 4000:
    xs = [i, i + 1, i + 2]
    total = total + xs[0]
    i = i + 1
""",
    "retained-list": """
acc = []
i = 0
while i < 4000:
    acc.append(i)
    i = i + 1
""",
    "transient-dict": """
total = 0
i = 0
while i < 3000:
    d = {"a": i, "b": i + 1}
    total = total + d["a"]
    i = i + 1
""",
    "string-concat": """
i = 0
n = 0
while i < 3000:
    s = "x" + str(i)
    n = n + len(s)
    i = i + 1
""",
    "call-heavy": """
def mk(i):
    return [i, i * 2]

total = 0
i = 0
while i < 3000:
    total = total + mk(i)[1]
    i = i + 1
""",
    "scalar-only": """
total = 0
i = 0
while i < 20000:
    total = total + i * 2
    i = i + 1
""",
}


def _stack_register_arrays():
    """Every active frame's `regs`, off the interpreter's own call stack.

    Only frames of `interp`'s own module are considered, and only the local
    named `regs` (plus the argument lists a call is mid-way through
    building), so this cannot pick up an unrelated list and overstate what
    is live.
    """
    out = []
    depth = 0
    try:
        f = sys._getframe(1)
    except ValueError:
        return out
    while f is not None and depth < 200000:
        g = f.f_globals.get("__name__", "")
        if g.endswith("minipy.interp"):
            for nm in ("regs", "callargs", "cargs", "fargs", "args"):
                v = f.f_locals.get(nm)
                if isinstance(v, list):
                    out.append(v)
        f = f.f_back
        depth += 1
    return out


def _trace(st, extra_roots):
    """Reachable `st.heap` indices, from the roots. A straight worklist.

    A `V` whose tag names a container carries a heap index in `iv`; anything
    else is an immediate and terminates the walk. That is the whole of the
    tracing rule, and it is short because the value model is uniform -- one
    16-byte tag+union, self-describing.
    """
    container_tags = set()
    for nm in ("T_LIST", "T_DICT", "T_SET", "T_OBJ", "T_TUPLE", "T_STR"):
        t = getattr(interp, nm, None)
        if isinstance(t, int):
            container_tags.add(t)
    nheap = len(st.heap)

    def is_ref(v):
        # Tag-based where the interpreter exposes its tags; otherwise fall
        # back to "an index that addresses a real slot". The fallback can
        # only over-count (call something live that is not), which keeps
        # this measurement honest: it never flatters the collector.
        tag = getattr(v, "tag", None)
        iv = getattr(v, "iv", None)
        if not isinstance(iv, int) or iv < 0 or iv >= nheap:
            return False
        if container_tags:
            return tag in container_tags
        return tag not in (0, 1, 2)      # none / int / float are immediates

    live = set()
    work = []

    def push_v(v):
        if is_ref(v) and v.iv not in live:
            live.add(v.iv)
            work.append(v.iv)

    def push_seq(seq):
        for v in seq or ():
            if hasattr(v, "tag"):
                push_v(v)

    push_seq(st.glob)
    push_seq(getattr(st, "mcache_cls", []))
    push_seq(getattr(st, "mcache_fidx", []))
    if hasattr(getattr(st, "exc_val", None), "tag"):
        push_v(st.exc_val)
    for arr in st.regpool or ():
        push_seq(arr)
    for arr in extra_roots:
        push_seq(arr)

    while work:
        i = work.pop()
        c = st.heap[i]
        push_seq(getattr(c, "items", None))
    return live


def measure(name, src, interval=100):
    """Run `src`, sampling the live fraction as it goes.

    Sampling is driven from `_heap_put` -- every Nth allocation, trace. That
    ties the sample points to allocation rather than to time, which is what
    the collector's own trigger would do.
    """
    hook = rpy.json.generate_decoder(interp.Program)
    prog = json.loads(json.dumps(compiler.compile_source(src)),
                      object_hook=hook)

    orig = interp._heap_put
    state = {"n": 0, "peak": 0, "worst": None, "rows": []}

    def counting(st, kind, items):
        r = orig(st, kind, items)
        n = len(st.heap)
        state["n"] += 1
        if n > state["peak"]:
            state["peak"] = n
        if state["n"] % interval == 0 and n > 0:
            live = len(_trace(st, _stack_register_arrays()))
            state["rows"].append((n, live))
            frac = live / float(n)
            if state["worst"] is None or frac < state["worst"][2]:
                state["worst"] = (n, live, frac)
        return r

    interp._heap_put = counting
    try:
        interp.interp_run(prog, [])
    finally:
        interp._heap_put = orig

    if not state["rows"]:
        # Too few allocations for the interval to fire. Sample once at the
        # end instead of reporting nothing: a program that allocates almost
        # nothing is a real answer (the collector has no work to do), not a
        # missing measurement.
        return (name, state["peak"], None, None)
    lo = state["worst"]
    return (name, state["peak"], lo[1], lo[2])


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            items = [(os.path.basename(sys.argv[1]), f.read())]
    else:
        items = sorted(PROGRAMS.items())

    print("%-16s %10s %10s %9s  %s"
          % ("program", "slots", "live", "live%", "verdict"))
    print("-" * 62)
    for name, src in items:
        try:
            nm, peak, live, frac = measure(name, src)
        except Exception as e:                        # noqa: BLE001
            print("%-16s  (skipped: %s)" % (name, str(e)[:34]))
            continue
        if live is None:
            # Not a failure to measure: a program that allocates almost no
            # containers is one the collector would never run for, which is
            # the property the doubling trigger has to preserve.
            print("%-16s %10d %10s %9s  nothing to collect"
                  % (nm, peak, "-", "-"))
            continue
        verdict = ("reclaimable" if frac < 0.25
                   else "mostly live" if frac > 0.75 else "mixed")
        print("%-16s %10d %10d %8.1f%%  %s"
              % (nm, peak, live, 100.0 * frac, verdict))
    print()
    print("live% is measured at the worst sample -- the moment the heap held")
    print("the most garbage. A low number is the collector's opportunity;")
    print("a program with no slots at all is one it would never run for.")


if __name__ == "__main__":
    main()
