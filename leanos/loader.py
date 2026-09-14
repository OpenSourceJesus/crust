"""LeanOS: admitting an ELF into the region list, in rpython.

`elfcheck.py` decides whether an image's headers are ones a loader should
map: loads ascending and disjoint, entry inside one, register class the
scheduler sizes.  LeanOS asks one more thing of it, and it is the founding
rule applied to the guest: the loads, taken as regions owned by the guest,
must leave the region list disjoint.  So `admit` is `accept_image` followed
by `regions_disjoint` on the list with the loads appended, and its theorems
are those two theorems, joined -- the ELF theorem and the memory theorem
are one theorem because the loader makes them one check.

  extended    xs with ys appended -- the region list grown by the loads
  claimed     owners grown by `guest`, once per load
  admit       1 if the image is accepted and the grown list is disjoint

Both guards are written `== 1`, so that each guard is the theorem about it.
"""


def extended(xs: "list[i64]", ys: "list[i64]") -> "list[i64]":
    """`xs` followed by `ys`."""
    out: "list[i64]" = []
    i = 0
    while i < len(xs):
        out.append(xs[i])
        i = i + 1
    j = 0
    while j < len(ys):
        out.append(ys[j])
        j = j + 1
    return out


def claimed(owners: "list[int]", guest: int, count: int) -> "list[int]":
    """`owners` followed by `guest`, `count` times."""
    out: "list[int]" = []
    i = 0
    while i < len(owners):
        out.append(owners[i])
        i = i + 1
    j = 0
    while j < count:
        out.append(guest)
        j = j + 1
    return out


def admit(bases: "list[i64]", sizes: "list[i64]",
          vaddrs: "list[i64]", memszs: "list[i64]",
          entry: "i64", cls: int) -> int:
    """1 to map the image and own its loads, 0 to refuse."""
    if accept_image(vaddrs, memszs, entry, cls) == 1:
        if regions_disjoint(extended(bases, vaddrs), extended(sizes, memszs)) == 1:
            return 1
    return 0
