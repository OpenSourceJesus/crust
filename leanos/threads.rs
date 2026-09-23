// LeanOS: threads, as records over the region list, in Rust.
//
// uses: memmap.rs
//
// A port of `leanos/threads.py`.  A thread is an owner in `memmap.rs` --
// owner `tid + 1`, since owner 0 is the kernel -- and a stack pointer.
// Every function here is a decision or a guarded update, and every guard is
// `region_of`, so the theorems about threads are consequences of the region
// list's, not new facts about memory.  RosettaMath's `threads_eq.py`
// proves `push_keeps_sp_ok` about a hand-typed model; here it is
// `sp_after_push`'s contract, proved about the lifted source:
//
//   push_keeps_sp_ok   if the stack pointer was in the thread's own region,
//                      it still is after `sp_after_push`, however large `n`.
//                      The guard is the invariant, so preservation is the
//                      guard.
//
// `sp_after_push` refusing rather than faulting is the design: a stack
// overflow in LeanOS is a decision the kernel makes with the region list in
// hand, not a page fault it discovers afterwards.
//
// Where the port differs from the Python, no input can make the Rust
// panic: `tid + 1` is formed only for `tid < usize::MAX`, and `sp_ok`
// answers 0 for the one `tid` without an owner id, which is the Python's
// answer too -- an owner past every index owns nothing.

// The owner id of thread `tid`; the kernel is owner 0.
#[requires(tid < usize::MAX)]
#[ensures(result == tid + 1)]
pub fn thread_owner(tid: usize) -> usize {
    tid + 1
}

// 1 if `sp` is inside a region thread `tid` owns.
#[ensures(result <= 1)]
pub fn sp_ok(bases: &[u64], sizes: &[u64], owners: &[usize], tid: usize,
             sp: u64) -> u32 {
    if tid >= usize::MAX {
        return 0;
    }
    if region_of(bases, sizes, owners, thread_owner(tid), sp) == 1 {
        return 1;
    }
    0
}

// 1 if every thread's stack pointer is in a region it owns.
#[ensures(result <= 1)]
pub fn all_sps_ok(bases: &[u64], sizes: &[u64], owners: &[usize],
                  sps: &[u64]) -> u32 {
    let mut i: usize = 0;
    #[invariant(i <= sps.len())]
    #[variant(sps.len() - i)]
    while i < sps.len() {
        if sp_ok(bases, sizes, owners, i, sps[i]) == 0 {
            return 0;
        }
        i += 1;
    }
    1
}

// The stack pointer after pushing `n` bytes, or unchanged if it would leave
// the thread's region.  `n <= sp` first, so that `sp - n` cannot underflow.
#[ensures(sp_ok(bases, sizes, owners, tid, sp) == 0
          || sp_ok(bases, sizes, owners, tid, result) == 1)]
pub fn sp_after_push(bases: &[u64], sizes: &[u64], owners: &[usize],
                     tid: usize, sp: u64, n: u64) -> u64 {
    if n <= sp {
        if sp_ok(bases, sizes, owners, tid, sp - n) == 1 {
            return sp - n;
        }
    }
    sp
}
