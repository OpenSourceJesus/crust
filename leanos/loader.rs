// LeanOS: admitting an ELF into the region list, in Rust.
//
// uses: elfcheck.rs memmap.rs
//
// A port of `leanos/loader.py`.  `elfcheck.rs` decides whether an image's
// headers are ones a loader should map; LeanOS asks one more thing, and it
// is the founding rule applied to the guest: the loads, taken as regions
// the guest owns, must leave the region list disjoint.  So `admit` is
// `accept_image` followed by `regions_disjoint` on the list with the loads
// appended, and its contracts are RosettaMath `loader_eq.py`'s theorems,
// proved here about the lifted source:
//
//   admit_accepts    admit == 1  ->  accept_image(..) == 1
//   admit_disjoint   admit == 1  ->  the grown region list is disjoint
//
// and, through `accept_image`, an admitted image's register class is one
// the scheduler sizes.
//
// The Python builds the grown lists (`extended`, `claimed`) and checks
// them.  Here the check reads the concatenation in place: `joined(xs, ys,
// k)` is position `k` of `xs` followed by `ys`, and
// `regions_disjoint_joined` is `regions_disjoint` over those positions --
// the same comparisons, in the same order, on the same values.  A function
// returning a new list is not in the proof fragment yet, and a loader that
// checks before it allocates is the better loader anyway.
//
// Where the port differs from the Python, no input can make the Rust
// panic: the two lengths are added only when the sum fits, and an image
// whose load list would not fit beside the region list in a `usize` is
// refused; region boundaries are compared as in `memmap.rs`, without
// forming `base + size`.

// Position `k` of `xs` followed by `ys`; 0 past the end of both.
pub fn joined(xs: &[u64], ys: &[u64], k: usize) -> u64 {
    if k < xs.len() {
        return xs[k];
    }
    if k - xs.len() < ys.len() {
        return ys[k - xs.len()];
    }
    0
}

// `regions_disjoint(bases ++ vaddrs, sizes ++ memszs)`, without building
// either list.
#[ensures(result <= 1)]
pub fn regions_disjoint_joined(bases: &[u64], sizes: &[u64],
                               vaddrs: &[u64], memszs: &[u64]) -> u32 {
    if vaddrs.len() > usize::MAX - bases.len() {
        return 0;
    }
    if memszs.len() > usize::MAX - sizes.len() {
        return 0;
    }
    let n: usize = bases.len() + vaddrs.len();
    if sizes.len() + memszs.len() < n {
        return 0;
    }
    let mut i: usize = 0;
    #[invariant(i <= n && n <= usize::MAX)]
    #[variant(n - i)]
    while i < n {
        if i > 0 {
            let here: u64 = joined(bases, vaddrs, i);
            let prev: u64 = joined(bases, vaddrs, i - 1);
            if here < prev {
                return 0;
            }
            if here - prev < joined(sizes, memszs, i - 1) {
                return 0;
            }
        }
        i += 1;
    }
    1
}

// 1 to map the image and own its loads, 0 to refuse.  Both guards are
// written `== 1`, so that each guard is the theorem about it.
#[ensures(result <= 1)]
#[ensures(result == 0 || accept_image(vaddrs, memszs, entry, cls) == 1)]
#[ensures(result == 0
          || regions_disjoint_joined(bases, sizes, vaddrs, memszs) == 1)]
#[ensures(result == 0 || cls <= 3)]
pub fn admit(bases: &[u64], sizes: &[u64], vaddrs: &[u64], memszs: &[u64],
             entry: u64, cls: u32) -> u32 {
    if accept_image(vaddrs, memszs, entry, cls) == 1 {
        if regions_disjoint_joined(bases, sizes, vaddrs, memszs) == 1 {
            return 1;
        }
    }
    0
}
