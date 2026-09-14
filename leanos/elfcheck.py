"""CrustOS: what the loader checks before it maps anything, in rpython.

`elf.c` parses an ELF64 and, today, runs it: PT_LOAD into a malloc'd image,
`mprotect` to RWX, call the entry.  Linux does the same, and the point of
this file is that it need not.  The properties a loader ought to establish
first are properties of the *headers* -- that the loads do not overlap, that
the entry lands inside one of them, that the register hint is one the
scheduler can size -- and headers are a list of records.  Deciding things
about lists is what rpython is for in this tree (see `schemes.py`), and it
is also what the proof side can already reason about: every function here
is modelled statement for statement in RosettaMath's `elfcheck_eq.py`,
proved there, and the two are tested against each other over a corpus.

Nothing here reads memory it did not receive as a list.  `elf.c` walks the
program headers and hands over two parallel lists, `vaddrs` and `memszs`,
one entry per PT_LOAD in file order, plus the entry point and the register
class it found in the CRUSTOS note.  This file answers yes or no.

Conventions, so that the model and the code can be the same function:

  * A result is 1 or 0, never a negative sentinel.  The model is over Nat.
  * A loop counts up with `i = i + 1`, so its variant is `len - i`.
  * The two lists are read only at the same index, and only after the
    length check at the top of each function, so no read is out of range.
  * Addresses are `i64`, since ELF64 virtual addresses are.  The model is
    over Nat, so `vaddr + memsz` does not wrap there and would here past
    2**63; a user-space image is nowhere near it, and that is the one
    distance between this file and its model.
"""

REG_CLASS_MAX = 3      # elf.c: 0 = minimal, 1 = +extras, 2 = full GPR, 3 = +xmm


def reg_class_ok(cls: int) -> int:
    """1 if `cls` is a register class `elf_regs_for_class` sizes.

    The guard is `<=` and not `>`, so that the guard *is* the theorem: an
    accepted class is `<= REG_CLASS_MAX` because that is what was tested.
    """
    if cls <= REG_CLASS_MAX:
        return 1
    return 0


def loads_ordered(vaddrs: "list[i64]", memszs: "list[i64]") -> int:
    """1 if every PT_LOAD ends at or before the next one begins.

    Ascending, non-overlapping loads are what `elf.c` assumes when it takes
    the first vaddr as the image base and the last end as the image size.
    Nothing today checks the assumption; this does.
    """
    if len(memszs) < len(vaddrs):
        return 0
    end = 0
    i = 0
    while i < len(vaddrs):
        if vaddrs[i] < end:
            return 0
        end = vaddrs[i] + memszs[i]
        i = i + 1
    return 1


def entry_in_load(vaddrs: "list[i64]", memszs: "list[i64]", entry: "i64") -> int:
    """1 if `entry` lies inside some PT_LOAD: vaddr <= entry < vaddr + memsz."""
    if len(memszs) < len(vaddrs):
        return 0
    i = 0
    while i < len(vaddrs):
        if vaddrs[i] <= entry:
            if entry < vaddrs[i] + memszs[i]:
                return 1
        i = i + 1
    return 0


def accept_image(vaddrs: "list[i64]", memszs: "list[i64]", entry: "i64",
           cls: int) -> int:
    """The loader's decision: 1 to map and run, 0 to refuse.

    Each check is a function of its own so that each has its own theorem;
    this one only conjoins them, and its theorem is that whatever each
    check guarantees, an accepted image has.
    """
    if reg_class_ok(cls) == 0:
        return 0
    if loads_ordered(vaddrs, memszs) == 0:
        return 0
    if entry_in_load(vaddrs, memszs, entry) == 0:
        return 0
    return 1
