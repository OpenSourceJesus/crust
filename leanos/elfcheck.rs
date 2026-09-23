// CrustOS: what the loader checks before it maps anything, in Rust.
//
// A port of `leanos/elfcheck.py`.  `elf.c` walks an ELF64's program headers
// and hands over two parallel lists, `vaddrs` and `memszs`, one entry per
// PT_LOAD in file order, plus the entry point and the register class from
// the CRUSTOS note.  This file answers yes or no: the loads ascend without
// overlapping, the entry lands inside one, and the register class is one
// `elf_regs_for_class` sizes.
//
// Two of RosettaMath's `elfcheck_eq.py` theorems are contracts here, proved
// about the lifted source rather than a hand-typed model:
//
//   reg_class_sized   `reg_class_ok`'s `result == 0 || cls <= 3`
//   accept_sized      `accept_image`'s `result == 0 || cls <= 3`
//
// Addresses are `u64`, since ELF64 virtual addresses are unsigned; the
// Python's `i64` was the distance its docstring owned up to.  Where the
// port differs from the Python, no input can make the Rust panic:
//
//   * `end = vaddrs[i] + memszs[i]` is formed only after `vaddrs[i] <=
//     u64::MAX - memszs[i]`.  A load that runs past the top of the address
//     space is refused; the Python, over unbounded integers, would have
//     accepted it, and no loader can map it.
//   * `entry < vaddrs[i] + memszs[i]` is `entry - vaddrs[i] < memszs[i]`,
//     behind `vaddrs[i] <= entry`: the same test, without the sum.
//
// A loop's invariant restates the length check made before it, since inside
// the loop the invariant and the loop condition are all a proof has.

// 1 if `cls` is a register class `elf_regs_for_class` sizes.  The guard is
// `<=` and not `>`, so that the guard *is* the theorem.
#[ensures(result <= 1)]
#[ensures(result == 0 || cls <= 3)]
pub fn reg_class_ok(cls: u32) -> u32 {
    if cls <= 3 {
        return 1;
    }
    0
}

// 1 if every PT_LOAD ends at or before the next one begins.
#[ensures(result <= 1)]
pub fn loads_ordered(vaddrs: &[u64], memszs: &[u64]) -> u32 {
    if memszs.len() < vaddrs.len() {
        return 0;
    }
    let mut end: u64 = 0;
    let mut i: usize = 0;
    #[invariant(i <= vaddrs.len() && vaddrs.len() <= memszs.len())]
    #[variant(vaddrs.len() - i)]
    while i < vaddrs.len() {
        if vaddrs[i] < end {
            return 0;
        }
        if vaddrs[i] > u64::MAX - memszs[i] {
            return 0;
        }
        end = vaddrs[i] + memszs[i];
        i += 1;
    }
    1
}

// 1 if `entry` lies inside some PT_LOAD: vaddr <= entry < vaddr + memsz.
#[ensures(result <= 1)]
pub fn entry_in_load(vaddrs: &[u64], memszs: &[u64], entry: u64) -> u32 {
    if memszs.len() < vaddrs.len() {
        return 0;
    }
    let mut i: usize = 0;
    #[invariant(i <= vaddrs.len() && vaddrs.len() <= memszs.len())]
    #[variant(vaddrs.len() - i)]
    while i < vaddrs.len() {
        if vaddrs[i] <= entry {
            if entry - vaddrs[i] < memszs[i] {
                return 1;
            }
        }
        i += 1;
    }
    0
}

// The loader's decision: 1 to map and run, 0 to refuse.  Each check is a
// function of its own so that each has its own theorem; this one conjoins
// them, and its theorem is that an accepted image has what each guarantees.
#[ensures(result <= 1)]
#[ensures(result == 0 || cls <= 3)]
pub fn accept_image(vaddrs: &[u64], memszs: &[u64], entry: u64,
                    cls: u32) -> u32 {
    if reg_class_ok(cls) == 0 {
        return 0;
    }
    if loads_ordered(vaddrs, memszs) == 0 {
        return 0;
    }
    if entry_in_load(vaddrs, memszs, entry) == 0 {
        return 0;
    }
    1
}
