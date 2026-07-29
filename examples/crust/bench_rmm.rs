int printf(const char *, ...);

/* Redox-shaped abstractions: an arch-generic page-flag type over a trait with
 * associated consts, a bitmap frame allocator, and a page-table walk. These
 * are the shapes rmm actually uses, so the benchmark measures what Crust
 * generates for real kernel code rather than for a scalar loop. */

trait Arch {
    const PAGE_SHIFT: u64;
    const ENTRY_FLAG_PRESENT: u64;
    const ENTRY_FLAG_WRITABLE: u64;
    const ENTRY_FLAG_NO_EXEC: u64;
    const PAGE_SIZE: u64 = 1 << Self::PAGE_SHIFT;
    const ENTRY_MASK: u64 = Self::PAGE_SIZE - 1;
}

struct X86_64;
impl Arch for X86_64 {
    const PAGE_SHIFT: u64 = 12;
    const ENTRY_FLAG_PRESENT: u64 = 1;
    const ENTRY_FLAG_WRITABLE: u64 = 2;
    const ENTRY_FLAG_NO_EXEC: u64 = 1 << 63;
}

struct PageFlags<A> { data: u64 }

impl<A> PageFlags<A> {
    fn new(addr: u64) -> PageFlags<A> {
        PageFlags { data: (addr & !A::ENTRY_MASK) | A::ENTRY_FLAG_PRESENT }
    }
    fn write(&mut self, on: bool) {
        if on { self.data |= A::ENTRY_FLAG_WRITABLE; }
        else { self.data &= !A::ENTRY_FLAG_WRITABLE; }
    }
    fn present(&self) -> bool { (self.data & A::ENTRY_FLAG_PRESENT) != 0 }
    fn frame(&self) -> u64 { (self.data & !A::ENTRY_MASK) >> A::PAGE_SHIFT }
}

struct Frames { words: [u64; 512], free: i64 }

impl Frames {
    fn init(&mut self) {
        self.free = 512 * 64;
        for i in 0..512 { self.words[i] = 0; }
    }
    fn taken(&self, f: u64) -> bool { (self.words[f / 64] >> (f % 64)) & 1 == 1 }
    fn take(&mut self, f: u64) {
        if !self.taken(f) { self.words[f / 64] |= 1 << (f % 64); self.free -= 1; }
    }
    fn release(&mut self, f: u64) {
        if self.taken(f) { self.words[f / 64] &= !(1 << (f % 64)); self.free += 1; }
    }
}

fn churn(fr: *mut Frames, rounds: i64) -> u64 {
    let mut acc: u64 = 0;
    for r in 0..rounds {
        let f: u64 = (r * 7919) % 32768;
        fr.take(f);
        let mut p: PageFlags<X86_64> = PageFlags::<X86_64>::new(f << 12);
        p.write(true);
        if p.present() { acc += p.frame(); }
        fr.release(f);
    }
    acc
}

fn main() {
    let mut fr: Frames = Frames { words: [0; 512], free: 0 };
    fr.init();
    printf("acc=%lu free=%ld\n", churn(&fr, 4000000), fr.free);
}
