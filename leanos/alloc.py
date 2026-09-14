"""LeanOS: the per-thread bump allocator, in rpython.

No shared heap.  A thread allocates from a region it owns -- its `heap`,
an index into the region list -- by moving a counter `used` up.  The
allocator is a guarded update like `sp_after_push`: it hands out `n` more
bytes if they fit and are the thread's to hand out, and otherwise leaves
`used` where it was.  Out of memory is a decision, not a fault, and it is
the kernel's decision with the region list in hand.

  bump        the new `used` after asking for `n` bytes, or the old one
  slot_addr   the address of the slot at `used`: base + used
  slot_ok     that address is inside region `heap` -- the slot handed out
              is the region's, which with `region_of_unique` makes it the
              thread's and no one else's

Conventions as in `memmap.py` and `threads.py`.  `used` is measured from
the region's base, so it is a Nat in the model with no subtraction.
"""


def bump(bases: "list[i64]", sizes: "list[i64]", owners: "list[int]",
         tid: int, heap: int, used: "i64", n: "i64") -> "i64":
    """`used + n` if that fits in region `heap` and `heap` is the thread's.

    The three guards are, in order, that `heap` names a region, that it is
    owned by this thread, and that the request fits.  The last one *is* the
    theorem `bump_bounded`: `used` never exceeds the region's size because
    that is what was tested.
    """
    if heap < len(bases):
        if owners[heap] == tid + 1:
            if used + n <= sizes[heap]:
                return used + n
    return used


def slot_addr(bases: "list[i64]", heap: int, used: "i64") -> "i64":
    """The address `used` bytes into region `heap`."""
    return bases[heap] + used


def slot_ok(bases: "list[i64]", sizes: "list[i64]", heap: int,
            used: "i64") -> int:
    """1 if the slot at `used` lies inside region `heap`."""
    if contains(bases, sizes, heap, bases[heap] + used) == 1:
        return 1
    return 0
