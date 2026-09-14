"""LeanOS: threads, as records over the region list, in rpython.

A thread is an owner in `memmap.py` -- owner `tid + 1`, since owner 0 is the
kernel -- and a stack pointer.  That is the whole record for now: what a
thread *is*, for the purpose of the founding rule, is the set of addresses
`region_of` says it may touch, and where its stack currently points.

Every function here is a decision or a guarded update, and every guard is
`region_of`.  So the theorems about threads are corollaries of
`region_of_unique`, not new facts about memory:

  sp_ok            the thread's stack pointer is in a region it owns
  all_sps_ok       every thread's is, over the table
  sp_after_push    move the stack pointer down by n, if the result is still
                   in the thread's own region; otherwise leave it where it
                   was.  The guard *is* the invariant, so preservation is
                   the guard.

`sp_after_push` refusing rather than faulting is the design: a stack
overflow in LeanOS is a decision the kernel makes with the region list in
hand, not a page fault it discovers afterwards.

Conventions as in `memmap.py`: results are 1 or 0 or a value, loops count
up, lists are read only behind a length check, and `i - 1` never appears.
"""

def thread_owner(tid: int) -> int:
    """The owner id of thread `tid`; the kernel is owner 0."""
    return tid + 1


def sp_ok(bases: "list[i64]", sizes: "list[i64]", owners: "list[int]",
          tid: int, sp: "i64") -> int:
    """1 if `sp` is inside a region thread `tid` owns."""
    if region_of(bases, sizes, owners, tid + 1, sp) == 1:
        return 1
    return 0


def all_sps_ok(bases: "list[i64]", sizes: "list[i64]", owners: "list[int]",
               sps: "list[i64]") -> int:
    """1 if every thread's stack pointer is in a region it owns."""
    i = 0
    while i < len(sps):
        if sp_ok(bases, sizes, owners, i, sps[i]) == 0:
            return 0
        i = i + 1
    return 1


def sp_after_push(bases: "list[i64]", sizes: "list[i64]", owners: "list[int]",
                  tid: int, sp: "i64", n: "i64") -> "i64":
    """The stack pointer after pushing `n` bytes, or unchanged if it would leave
    the thread's region.

    `n <= sp` first, so that `sp - n` is the same number here and in the
    model, where subtraction truncates at zero.
    """
    if n <= sp:
        if sp_ok(bases, sizes, owners, tid, sp - n) == 1:
            return sp - n
    return sp
