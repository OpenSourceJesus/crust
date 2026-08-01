// alloc.rs -- the kernel heap's block bookkeeping.
//
// alloc.c owns the arena: one static byte array, and the translation between a
// block offset and a real pointer. Everything about *which* bytes are in use
// lives here, because it is index arithmetic and a checker can see all of it.
// This is the same split as editbuf.rs: no hardware contact, so no C.
//
// Representation: the arena is covered, with no gaps and no overlaps, by
// `count` blocks held in address order in parallel arrays. Block i occupies
// [off[i], off[i] + size[i]). That invariant is the whole design -- it makes
// coalescing a neighbour check rather than a search, and it makes the
// allocator auditable by walking one list.
//
// Allocation is first-fit with splitting; freeing merges with the block on
// either side. Sizes and offsets are multiples of ALIGN, so an aligned arena
// base means every returned pointer is aligned and no block ever needs a
// leading pad.
//
//   rustc     checks it            -- `make check-rs`
//   crust.py  lowers it to C       -- gcc builds the result into the kernel
//
// Written in the subset both accept: flat arrays, explicit index math, no
// slices or iterators or Option.

const MAX_BLOCKS: i32 = 512;
const ALIGN: i32 = 16;

struct Heap {
    off: [i32; 512],
    size: [i32; 512],
    used: [u8; 512],
    count: i32,
    total: i32,
    fail: i32, // allocations refused, for the `mem` command
}

impl Heap {
    // Round up to the alignment. Returns 0 for a non-positive request so the
    // caller's "size <= 0" check is the only place that has to care.
    fn round_up(&self, n: i32) -> i32 {
        if n <= 0 {
            return 0;
        }
        return ((n + ALIGN - 1) / ALIGN) * ALIGN;
    }

    fn init(&mut self, total: i32) {
        let t: i32 = (total / ALIGN) * ALIGN;
        self.total = t;
        self.count = 1;
        self.fail = 0;
        self.off[0] = 0;
        self.size[0] = t;
        self.used[0] = 0;
    }

    fn block_count(&self) -> i32 {
        return self.count;
    }

    fn capacity(&self) -> i32 {
        return self.total;
    }

    fn failures(&self) -> i32 {
        return self.fail;
    }

    fn block_off(&self, i: i32) -> i32 {
        if i < 0 || i >= self.count {
            return -1;
        }
        return self.off[i as usize];
    }

    fn block_size(&self, i: i32) -> i32 {
        if i < 0 || i >= self.count {
            return 0;
        }
        return self.size[i as usize];
    }

    fn block_used(&self, i: i32) -> bool {
        if i < 0 || i >= self.count {
            return false;
        }
        return self.used[i as usize] != 0;
    }

    fn bytes_used(&self) -> i32 {
        let mut n: i32 = 0;
        let mut i: i32 = 0;
        while i < self.count {
            if self.used[i as usize] != 0 {
                n += self.size[i as usize];
            }
            i += 1;
        }
        return n;
    }

    // Size of the largest free run. Reported by `mem` because total-free on its
    // own hides fragmentation, which is the failure mode that actually bites.
    fn largest_free(&self) -> i32 {
        let mut best: i32 = 0;
        let mut i: i32 = 0;
        while i < self.count {
            if self.used[i as usize] == 0 && self.size[i as usize] > best {
                best = self.size[i as usize];
            }
            i += 1;
        }
        return best;
    }

    // Open a slot at `at` by shifting the tail right one place.
    fn insert_at(&mut self, at: i32) -> bool {
        if self.count >= MAX_BLOCKS {
            return false;
        }
        if at < 0 || at > self.count {
            return false;
        }
        let mut i: i32 = self.count;
        while i > at {
            self.off[i as usize] = self.off[(i - 1) as usize];
            self.size[i as usize] = self.size[(i - 1) as usize];
            self.used[i as usize] = self.used[(i - 1) as usize];
            i -= 1;
        }
        self.count += 1;
        return true;
    }

    // Drop the slot at `at`, shifting the tail left one place.
    fn remove_at(&mut self, at: i32) -> bool {
        if at < 0 || at >= self.count {
            return false;
        }
        let mut i: i32 = at;
        while i < self.count - 1 {
            self.off[i as usize] = self.off[(i + 1) as usize];
            self.size[i as usize] = self.size[(i + 1) as usize];
            self.used[i as usize] = self.used[(i + 1) as usize];
            i += 1;
        }
        self.count -= 1;
        return true;
    }

    // First fit. Returns the offset of the allocation, or -1.
    //
    // A block big enough to hold the request plus at least one more aligned
    // unit is split; otherwise the whole block is handed over, so the leftover
    // never becomes an unusable zero-size entry.
    fn alloc(&mut self, want: i32) -> i32 {
        let n: i32 = self.round_up(want);
        if n <= 0 {
            self.fail += 1;
            return -1;
        }

        let mut i: i32 = 0;
        while i < self.count {
            if self.used[i as usize] == 0 && self.size[i as usize] >= n {
                let whole: i32 = self.size[i as usize];
                if whole >= n + ALIGN {
                    if !self.insert_at(i + 1) {
                        // Out of block slots: hand over the whole block rather
                        // than fail an allocation the arena can satisfy.
                        self.used[i as usize] = 1;
                        return self.off[i as usize];
                    }
                    self.off[(i + 1) as usize] = self.off[i as usize] + n;
                    self.size[(i + 1) as usize] = whole - n;
                    self.used[(i + 1) as usize] = 0;
                    self.size[i as usize] = n;
                }
                self.used[i as usize] = 1;
                return self.off[i as usize];
            }
            i += 1;
        }

        self.fail += 1;
        return -1;
    }

    fn find(&self, offset: i32) -> i32 {
        let mut i: i32 = 0;
        while i < self.count {
            if self.off[i as usize] == offset {
                return i;
            }
            i += 1;
        }
        return -1;
    }

    // Free by offset, then merge with the neighbour on each side. Merging the
    // right neighbour first keeps `i` valid for the left merge.
    //
    // Returns false for an offset that is not the start of a live block, which
    // covers both a double free and a bad pointer.
    fn free(&mut self, offset: i32) -> bool {
        let i: i32 = self.find(offset);
        if i < 0 {
            return false;
        }
        if self.used[i as usize] == 0 {
            return false;
        }
        self.used[i as usize] = 0;

        if i + 1 < self.count && self.used[(i + 1) as usize] == 0 {
            self.size[i as usize] += self.size[(i + 1) as usize];
            self.remove_at(i + 1);
        }
        if i > 0 && self.used[(i - 1) as usize] == 0 {
            self.size[(i - 1) as usize] += self.size[i as usize];
            self.remove_at(i);
        }
        return true;
    }

    // Walk the block list and confirm the covering invariant still holds:
    // blocks are in address order, start at 0, leave no gap, and sum to the
    // arena size. Returns 0 when consistent, else the 1-based index of the
    // first block that breaks it.
    //
    // This exists so `mem check` can audit the heap from the shell. An
    // allocator that cannot be inspected on the machine it runs on is one that
    // gets debugged by guesswork.
    fn verify(&self) -> i32 {
        if self.count <= 0 || self.count > MAX_BLOCKS {
            return 1;
        }
        let mut expect: i32 = 0;
        let mut i: i32 = 0;
        while i < self.count {
            if self.off[i as usize] != expect {
                return i + 1;
            }
            if self.size[i as usize] <= 0 {
                return i + 1;
            }
            if self.size[i as usize] % ALIGN != 0 {
                return i + 1;
            }
            // Two adjacent free blocks mean a coalesce was missed.
            if i > 0
                && self.used[i as usize] == 0
                && self.used[(i - 1) as usize] == 0
            {
                return i + 1;
            }
            expect += self.size[i as usize];
            i += 1;
        }
        if expect != self.total {
            return self.count + 1;
        }
        return 0;
    }
}
