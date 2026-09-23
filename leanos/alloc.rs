// LeanOS: the per-thread bump allocator, in Rust.
//
// A port of `leanos/alloc.py`, and a LeanOS module whose model is generated
// from the file that ships: `shivyc/rustproof.py` lifts each function with
// its contract, and `tests/test_rustproof.py` proves `bump_bounded` and
// `bump_monotone` from the lifted source -- the theorems RosettaMath's
// `alloc_eq.py` proves about a model it re-types by hand.
//
// No shared heap. A thread allocates from a region it owns -- its `heap`,
// an index into the region list -- by moving a counter `used` up. Out of
// memory is a decision, not a fault.
//
// Where the port differs from the Python, it is so that no input can make
// the Rust panic -- an index out of bounds, a `u32` or `u64` passing its
// maximum, a subtraction below zero -- and every difference gives the
// Python's result on every input:
//
//   * Every array is read behind its own length check. The Python reads
//     `owners[heap]` and `sizes[heap]` behind `heap < len(bases)` alone,
//     and its model's `nth` quietly answers 0 past the end; here each read
//     has the check that makes it in bounds.
//   * `owners[heap] == tid + 1` is `owners[heap] >= 1 &&
//     owners[heap] - 1 == tid`: `tid + 1` overflows at `u32::MAX`.
//   * In `contains`, `addr < base + size` is `addr - base < size`, which
//     is the same comparison once `base <= addr`, and cannot overflow.
//   * In `bump`, the test `used + n <= size` is `n <= size && used <=
//     size - n`: the same answer wherever `used + n` fits, and the only
//     honest one where it does not.  The first port evaluated `used + n`
//     to decide whether it fitted, so `used = n = 2^63` wrapped to 0,
//     passed, and returned 0 -- which Crust's runtime check caught as
//     `ensures used <= result` violated.  The safety lift had named the
//     same addition as an open obligation; it was open because it was
//     false.
//   * In `slot_ok`, an address past `u64::MAX` is in no region, so it
//     answers 0 before forming one.  `slot_addr` cannot answer anything
//     but an address, so it asks its caller for that fact instead.
//
// Nothing is owed: every obligation the safety lift states is proved, and
// the proofs are checked again by Lean 4.

pub fn contains(bases: &[u64], sizes: &[u64], i: usize, addr: u64) -> u32 {
    if i >= bases.len() {
        return 0;
    }
    if sizes.len() < bases.len() {
        return 0;
    }
    if i >= sizes.len() {
        return 0;
    }
    if bases[i] <= addr {
        if addr - bases[i] < sizes[i] {
            return 1;
        }
    }
    0
}

// `used + n` if that fits in region `heap` and `heap` is the thread's.
// The last guard *is* `bump_bounded`: `used` never passes the region's
// size because that is what was tested.
#[ensures(used <= result)]
pub fn bump(bases: &[u64], sizes: &[u64], owners: &[u32], tid: u32,
            heap: usize, used: u64, n: u64) -> u64 {
    if heap < bases.len() && heap < owners.len() && heap < sizes.len() {
        if owners[heap] >= 1 && owners[heap] - 1 == tid {
            if n <= sizes[heap] && used <= sizes[heap] - n {
                return used + n;
            }
        }
    }
    used
}

// The address `used` bytes into region `heap`.  The caller knows the slot
// is inside the region, and the memory map places every region below
// `u64::MAX`; that is what the second clause asks.
#[requires(heap < bases.len())]
#[requires(bases[heap] <= u64::MAX - used)]
pub fn slot_addr(bases: &[u64], heap: usize, used: u64) -> u64 {
    bases[heap] + used
}

// 1 if the slot at `used` lies inside region `heap`.
#[ensures(result <= 1)]
pub fn slot_ok(bases: &[u64], sizes: &[u64], heap: usize, used: u64) -> u32 {
    if heap >= bases.len() {
        return 0;
    }
    if bases[heap] > u64::MAX - used {
        return 0;
    }
    if contains(bases, sizes, heap, bases[heap] + used) == 1 {
        return 1;
    }
    0
}
