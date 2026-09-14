"""LeanOS: the region list, in rpython.

Every region the kernel will touch is an entry here -- base, size, owner --
laid out at build time.  `regions_disjoint` is the one theorem the rest of
LeanOS rests on: if it says 1, no two regions overlap, and a thread that
was allocated from its own region cannot reach another's.

Written the way `crustos/elfcheck.py` is, and for the same reason: it
decides, never dereferences; every result is 1 or 0; every loop counts up;
every list is read only behind a length check.  `regions_disjoint` *is*
`loads_ordered` with an owner column, so the loop theorem `elfcheck.py`
still owes is the same theorem this file owes, and it is proved once.

Owners are small integers: 0 is the kernel, 1.. are threads.  `owned_by`
is the region count for an owner, which is what a thread's allocator will
be bounded by.
"""

OWNER_KERNEL = 0


def regions_disjoint(bases: "list[i64]", sizes: "list[i64]") -> int:
    """1 if the regions are ascending and no two overlap.

    Touching is fine: a region may end exactly where the next begins.
    Ascending order is a convention the build imposes so that disjointness
    is one pass rather than a quadratic one, and so that the proof is a
    loop invariant rather than a statement about every pair.

    The previous region's end is recomputed from `i` rather than carried
    in a variable, so that the invariant the proof carries -- "every pair
    checked so far was in order" -- is the guard itself, written once, and
    not an equation between a variable and what it was meant to hold.
    """
    if len(sizes) < len(bases):
        return 0
    i = 0
    while i < len(bases):
        if i > 0:
            if bases[i] < bases[i - 1] + sizes[i - 1]:
                return 0
        i = i + 1
    return 1


def contains(bases: "list[i64]", sizes: "list[i64]", i: int,
             addr: "i64") -> int:
    """1 if `addr` lies in region `i`: base <= addr < base + size."""
    if i >= len(bases):
        return 0
    if len(sizes) < len(bases):
        return 0
    if bases[i] <= addr:
        if addr < bases[i] + sizes[i]:
            return 1
    return 0


def owned_by(owners: "list[int]", who: int) -> int:
    """How many regions `who` owns."""
    n = 0
    i = 0
    while i < len(owners):
        if owners[i] == who:
            n = n + 1
        i = i + 1
    return n


def region_index(bases: "list[i64]", sizes: "list[i64]", owners: "list[int]",
                 who: int, addr: "i64") -> int:
    """The index of the region `who` owns that holds `addr`, or len(bases).

    Returns the *witness*, not a yes or no, for the same reason `scheme_of`
    returns an index and not a flag: a proof about which region was found
    cannot be written against a Bool.  `region_of` below is the decision,
    and is this compared against the length.
    """
    if len(sizes) < len(bases):
        return len(bases)
    if len(owners) < len(bases):
        return len(bases)
    i = 0
    while i < len(bases):
        if owners[i] == who:
            if bases[i] <= addr:
                if addr < bases[i] + sizes[i]:
                    return i
        i = i + 1
    return len(bases)


def region_of(bases: "list[i64]", sizes: "list[i64]", owners: "list[int]",
              who: int, addr: "i64") -> int:
    """1 if `addr` lies in a region `who` owns; the founding access rule.

    A thread may touch an address only if this says 1.  With the regions
    proved disjoint, 1 for one owner is 0 for every other.
    """
    if region_index(bases, sizes, owners, who, addr) < len(bases):
        return 1
    return 0
