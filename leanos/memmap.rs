// LeanOS: the region list, in Rust.
//
// A port of `leanos/memmap.py`.  Every region the kernel will touch is an
// entry here -- base, size, owner -- laid out at build time, and
// `regions_disjoint` is the check the rest of LeanOS rests on: if it says 1,
// no two regions overlap, and a thread allocating from its own region
// cannot reach another's.  `shivyc/rustproof.py` lifts each function with
// its contract, and `tools/rustprove.py` proves the contracts and every
// place the Rust could panic, through each loop by the loop's own
// `#[invariant]` and `#[variant]`.
//
// Owners are small integers: 0 is the kernel, 1.. are threads.  They are
// `usize` because a thread's id is also an index into the thread table
// (`threads.rs`), and owner `tid + 1` then fits exactly when `tid` is an
// index: `alloc.rs`, proved on its own, keeps `u32`.
//
// Where the port differs from the Python, it is so that no input can make
// the Rust panic, and each difference gives the Python's answer wherever
// the Python's arithmetic did not pass 2^64:
//
//   * `bases[i] < bases[i - 1] + sizes[i - 1]` is `bases[i] < bases[i - 1]
//     || bases[i] - bases[i - 1] < sizes[i - 1]`.  Over the integers these
//     are the same test; in a `u64` the first overflows for a region ending
//     at the top of the address space, and then answers wrongly.
//   * `addr < bases[i] + sizes[i]` is `addr - bases[i] < sizes[i]`, behind
//     `bases[i] <= addr`, as in `alloc.rs`'s `contains`.
//
// `i - 1` is read only behind `i > 0`, as in the Python; that `i - 1` is
// then an index in range is `sub_one_lt`, proved in RosettaMath's prelude.
//
// A loop's invariant also restates the length checks made before it: inside
// the loop, the invariant and the loop condition are all a proof has, so
// `sizes[i]` is in range only if the invariant still says the length check
// passed.

// 1 if the regions are ascending and no two overlap.  Touching is fine: a
// region may end exactly where the next begins.
#[ensures(result <= 1)]
pub fn regions_disjoint(bases: &[u64], sizes: &[u64]) -> u32 {
    if sizes.len() < bases.len() {
        return 0;
    }
    let mut i: usize = 0;
    #[invariant(i <= bases.len() && bases.len() <= sizes.len())]
    #[variant(bases.len() - i)]
    while i < bases.len() {
        if i > 0 {
            if bases[i] < bases[i - 1] {
                return 0;
            }
            if bases[i] - bases[i - 1] < sizes[i - 1] {
                return 0;
            }
        }
        i += 1;
    }
    1
}

// 1 if `addr` lies in region `i`: base <= addr < base + size.
#[ensures(result <= 1)]
pub fn contains(bases: &[u64], sizes: &[u64], i: usize, addr: u64) -> u32 {
    if i >= bases.len() {
        return 0;
    }
    if sizes.len() < bases.len() {
        return 0;
    }
    if bases[i] <= addr {
        if addr - bases[i] < sizes[i] {
            return 1;
        }
    }
    0
}

// How many regions `who` owns.
#[ensures(result <= owners.len())]
pub fn owned_by(owners: &[usize], who: usize) -> usize {
    let mut n: usize = 0;
    let mut i: usize = 0;
    #[invariant(i <= owners.len() && n <= i)]
    #[variant(owners.len() - i)]
    while i < owners.len() {
        if owners[i] == who {
            n += 1;
        }
        i += 1;
    }
    n
}

// The index of the region `who` owns that holds `addr`, or `bases.len()`.
// The witness, not a yes or no: a proof about which region was found cannot
// be written against a Bool.  `region_of` is the decision.  The second
// contract is RosettaMath's `region_index_found`: an index it returns is one
// it vouches for -- owned by `who`, and holding `addr`.
#[ensures(result <= bases.len())]
#[ensures(result == bases.len()
          || (owners[result] == who && bases[result] <= addr
              && addr - bases[result] < sizes[result]))]
pub fn region_index(bases: &[u64], sizes: &[u64], owners: &[usize], who: usize,
                    addr: u64) -> usize {
    if sizes.len() < bases.len() {
        return bases.len();
    }
    if owners.len() < bases.len() {
        return bases.len();
    }
    let mut i: usize = 0;
    #[invariant(i <= bases.len() && bases.len() <= sizes.len()
                && bases.len() <= owners.len())]
    #[variant(bases.len() - i)]
    while i < bases.len() {
        if owners[i] == who {
            if bases[i] <= addr {
                if addr - bases[i] < sizes[i] {
                    return i;
                }
            }
        }
        i += 1;
    }
    bases.len()
}

// 1 if `addr` lies in a region `who` owns: the founding access rule.  A
// thread may touch an address only if this says 1.
#[ensures(result <= 1)]
pub fn region_of(bases: &[u64], sizes: &[u64], owners: &[usize], who: usize,
                 addr: u64) -> u32 {
    if region_index(bases, sizes, owners, who, addr) < bases.len() {
        return 1;
    }
    0
}
