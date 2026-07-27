"""The bookkeeping half of a mini OS, in rpython.

This is the language split the three-way mix exists for. Anything that is
list-shaped -- building a table, filtering it, sorting it -- is far shorter in
rpython than in the Crust subset, which has no iterator protocol and no
collections beyond a hand-written `Vec`. rpython already has both, and py2c
lowers them to plain C structs, so the result is something Rust can walk with
no conversion.

The other half of the split is in `mini_os.c`: anything that touches a
register, a fixed layout, or a hot loop stays in Rust or C, where there is no
allocator and no boxing between the code and the machine.
"""


def build_quanta(n: int, base: int) -> "list[int]":
    """Time slice for each of `n` priority levels, longest first."""
    out: "list[int]" = []
    i = 0
    while i < n:
        out.append(base * (n - i))
        i += 1
    return out


def runnable_pids(count: int, skip: int) -> "list[int]":
    """PIDs of the runnable tasks, skipping every `skip`-th one."""
    out: "list[int]" = []
    pid = 1
    while pid <= count:
        if skip <= 0 or pid % skip != 0:
            out.append(pid)
        pid += 1
    return out


def total_demand(n: int, base: int) -> int:
    """Sum of the quanta -- the work one full round costs."""
    total = 0
    for q in build_quanta(n, base):
        total += q
    return total
