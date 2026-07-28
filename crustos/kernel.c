/* CrustOS -- a small OS built from the parts of Redox that Crust compiles,
 * plus the parts we supply ourselves.
 *
 *   vendor/kernel   genuine Redox source, compiled by Crust and linked in:
 *                   rmm page tables and flags, buddy and bump frame
 *                   allocators, architecture constants. Those objects are on
 *                   the link line; `kernel_heap_offset` below is upstream's.
 *   schemes.py      the scheme layer, in rpython.
 *   this file       the frame allocator, context table, scheduler and
 *                   syscall dispatch, in Rust; boot in C.
 *
 * CrustOS runs hosted, as an ordinary program. There is no bootloader and no
 * bare-metal target, so this is a working model of the structure rather than
 * an OS you can boot. See CRUSTOS.md.
 */
#include "schemes.py"

int printf(const char *, ...);

/* Supplied by vendor/kernel/src/arch/x86/consts.rs, compiled by Crust. */
unsigned long kernel_heap_offset(void);

/* ------------------------------------------------------------------ */
/* Paging, in the shape rmm/src/page/flags.rs uses                      */
/*                                                                      */
/* Upstream's `PageFlags<A>` is generic over the architecture and reads  */
/* its constants off a trait: `A::ENTRY_FLAG_NO_EXEC`. Crust now         */
/* monomorphises that, so the same shape works here -- one set of flag   */
/* logic, one instantiation per architecture, resolved at compile time   */
/* with no dispatch.                                                     */
/* ------------------------------------------------------------------ */

trait Arch {
    // An associated type, in the shape rmm uses for `Deref::Target` and
    // `Iterator::Item`: each architecture names the integer width its page
    // table entries are, and generic code refers to it as `A::Entry`.
    type Entry;

    const ENTRY_FLAG_PRESENT: u64;
    const ENTRY_FLAG_WRITABLE: u64;
    const ENTRY_FLAG_NO_EXEC: u64;
    const ENTRY_FLAG_USER: u64;
    const PAGE_SHIFT: u64;

    // Defaults, as upstream's `Arch` declares them -- including one derived
    // from another constant of the same trait. An impl that does not override
    // these inherits them, with `Self::` resolved to the implementing type.
    const PAGE_SIZE: u64 = 1 << Self::PAGE_SHIFT;
    const PAGE_MASK: u64 = Self::PAGE_SIZE - 1;
    const PAGE_LEVELS: u64 = 4;
}

struct X86_64;
impl Arch for X86_64 {
    type Entry = u64;
    const ENTRY_FLAG_PRESENT: u64 = 1;
    const ENTRY_FLAG_WRITABLE: u64 = 1 << 1;
    const ENTRY_FLAG_NO_EXEC: u64 = 1 << 63;
    const ENTRY_FLAG_USER: u64 = 1 << 2;
    const PAGE_SHIFT: u64 = 12;
}

struct Aarch64;
impl Arch for Aarch64 {
    type Entry = u64;
    const ENTRY_FLAG_PRESENT: u64 = 3;
    const ENTRY_FLAG_WRITABLE: u64 = 0;
    const ENTRY_FLAG_NO_EXEC: u64 = 1 << 54;
    const ENTRY_FLAG_USER: u64 = 1 << 6;
    const PAGE_SHIFT: u64 = 16;          // 64K granule
    const PAGE_LEVELS: u64 = 3;          // overrides the trait default
}

struct PageFlags<A> {
    data: u64,
}

impl<A> PageFlags<A> {
    fn new() -> PageFlags<A> {
        PageFlags { data: A::ENTRY_FLAG_PRESENT | A::ENTRY_FLAG_NO_EXEC }
    }

    fn write(&mut self, on: bool) {
        if on {
            self.data |= A::ENTRY_FLAG_WRITABLE;
        } else {
            self.data &= !A::ENTRY_FLAG_WRITABLE;
        }
    }

    fn user(&mut self, on: bool) {
        if on {
            self.data |= A::ENTRY_FLAG_USER;
        } else {
            self.data &= !A::ENTRY_FLAG_USER;
        }
    }

    fn execute(&mut self, on: bool) {
        if on {
            self.data &= !A::ENTRY_FLAG_NO_EXEC;
        } else {
            self.data |= A::ENTRY_FLAG_NO_EXEC;
        }
    }

    fn bits(&self) -> u64 {
        self.data
    }

    fn present(&self) -> bool {
        (self.data & A::ENTRY_FLAG_PRESENT) != 0
    }

    /* Physical address of the frame this entry points at. The entry width
     * comes from the architecture's own associated type. */
    fn frame_of(addr: A::Entry) -> A::Entry {
        addr >> A::PAGE_SHIFT
    }

    fn entry_bits() -> A::Entry {
        (A::PAGE_SIZE - 1) as A::Entry
    }
}

const FRAMES: i64 = 512;
const MAX_CONTEXTS: usize = 16;
const MAX_FILES: usize = 16;

/* ------------------------------------------------------------------ */
/* Physical frames -- a bitmap, one bit per frame                       */
/* ------------------------------------------------------------------ */

struct Frames {
    words: [i64; 8],
    free: i64,
}

impl Frames {
    fn init(&mut self) {
        self.free = FRAMES;
        for i in 0..8 {
            self.words[i] = 0;
        }
    }

    fn taken(&self, f: i64) -> bool {
        (self.words[f / 64] >> (f % 64)) & 1 == 1
    }

    fn take(&mut self, f: i64) {
        if !self.taken(f) {
            self.words[f / 64] |= 1 << (f % 64);
            self.free -= 1;
        }
    }

    fn release(&mut self, f: i64) {
        if self.taken(f) {
            self.words[f / 64] &= !(1 << (f % 64));
            self.free += 1;
        }
    }

    fn alloc(&mut self) -> i64 {
        for f in 0..FRAMES {
            if !self.taken(f) {
                self.take(f);
                return f;
            }
        }
        -1
    }
}

/* ------------------------------------------------------------------ */
/* Contexts                                                            */
/* ------------------------------------------------------------------ */

#[derive(Clone, Copy, Default)]
struct Context {
    pid: i32,
    prio: i32,
    ticks: i64,
    frames: i32,
    state: i32,          /* 0 free, 1 runnable, 2 blocked, 3 exited */
}

/* ------------------------------------------------------------------ */
/* Syscalls -- a data-carrying enum, dispatched by `match`              */
/* ------------------------------------------------------------------ */

enum Call {
    Open { scheme: i32, owner: i32 },
    Read(i32, i32),
    Write(i32, i32),
    Close(i32),
    Alloc(i32, i32),
    Yield,
}

#[derive(Clone, Copy, Default)]
struct Fd {
    scheme: i32,
    owner: i32,
    bytes: i32,
}

struct Kernel {
    ctx: [Context; 16],
    fds: [Fd; 16],
    frames: Frames,
    used: i32,
    open_fds: i32,
}

impl Kernel {
    fn init(&mut self) {
        self.frames.init();
        self.used = 0;
        self.open_fds = 0;
        for i in 0..16 {
            self.ctx[i].pid = 0;
            self.ctx[i].prio = 0;
            self.ctx[i].ticks = 0;
            self.ctx[i].frames = 0;
            self.ctx[i].state = 0;
            self.fds[i].scheme = SCHEME_NONE;
            self.fds[i].owner = 0;
            self.fds[i].bytes = 0;
        }
    }

    fn spawn(&mut self, prio: i32) -> i32 {
        if self.used >= 16 {
            return -1;
        }
        let slot: i32 = self.used;
        self.used += 1;
        self.ctx[slot].pid = slot + 1;
        self.ctx[slot].prio = prio;
        self.ctx[slot].state = 1;
        self.ctx[slot].pid
    }

    fn block(&mut self, pid: i32) {
        let i: i32 = pid - 1;
        if i >= 0 && i < self.used {
            self.ctx[i].state = 2;
        }
    }

    fn runnable(&self) -> i32 {
        let mut n: i32 = 0;
        for i in 0..self.used {
            if self.ctx[i].state == 1 {
                n += 1;
            }
        }
        n
    }

    fn dispatch(&mut self, c: Call) -> i32 {
        match c {
            Call::Open { scheme, owner } => self.sys_open(scheme, owner),
            Call::Read(fd, n) => self.sys_io(fd, n),
            Call::Write(fd, n) => self.sys_io(fd, n),
            Call::Close(fd) => self.sys_close(fd),
            Call::Alloc(pid, n) => self.sys_alloc(pid, n),
            Call::Yield => 0,
        }
    }

    fn sys_open(&mut self, scheme: i32, owner: i32) -> i32 {
        if scheme < 0 || self.open_fds >= 16 {
            return -1;
        }
        let fd: i32 = self.open_fds;
        self.open_fds += 1;
        self.fds[fd].scheme = scheme;
        self.fds[fd].owner = owner;
        self.fds[fd].bytes = 0;
        fd
    }

    fn sys_io(&mut self, fd: i32, n: i32) -> i32 {
        if fd < 0 || fd >= self.open_fds {
            return -1;
        }
        self.fds[fd].bytes += n;
        self.fds[fd].bytes
    }

    fn sys_close(&mut self, fd: i32) -> i32 {
        if fd < 0 || fd >= self.open_fds {
            return -1;
        }
        self.fds[fd].scheme = SCHEME_NONE;
        0
    }

    fn sys_alloc(&mut self, pid: i32, n: i32) -> i32 {
        let slot: i32 = pid - 1;
        if slot < 0 || slot >= self.used {
            return -1;
        }
        let mut got: i32 = 0;
        for _i in 0..n {
            if self.frames.alloc() >= 0 {
                got += 1;
            }
        }
        self.ctx[slot].frames += got;
        got
    }

    /* Round robin, charging each runnable context its priority quantum. */
    fn schedule(&mut self, rounds: i32) -> i64 {
        let mut switches: i64 = 0;
        for _r in 0..rounds {
            for i in 0..self.used {
                if self.ctx[i].state == 1 {
                    self.ctx[i].ticks += (4 - self.ctx[i].prio) as i64;
                    switches += 1;
                }
            }
        }
        switches
    }
}

/* Route a batch of URLs in rpython, then open one descriptor per accepted
 * one. The routing table is walked as an ordinary `PyList<i32>`: three words
 * py2c built, read here with no conversion. */
fn open_batch(k: *mut Kernel, urls: *mut c_char, owner: i32) -> i32 {
    let kinds: *mut PyList<i32> = route_all(urls) as *mut PyList<i32>;
    let mut opened: i32 = 0;
    for kind in kinds {
        if kind != SCHEME_NONE {
            let call: Call = Call::Open { scheme: kind, owner: owner };
            if k.dispatch(call) >= 0 {
                opened += 1;
            }
        }
    }
    opened
}

/* C cannot name `PageFlags<X86_64>` -- an instantiation only exists if Rust
 * code mentions it -- so the entry points into the paging layer are Rust. */
fn kernel_page_bits() -> u64 {
    let mut p: PageFlags<X86_64> = PageFlags::<X86_64>::new();
    p.write(true);
    p.execute(false);
    p.bits()
}

fn kernel_page_present() -> bool {
    let p: PageFlags<X86_64> = PageFlags::<X86_64>::new();
    p.present()
}

fn user_page_bits_arm64() -> u64 {
    let mut p: PageFlags<Aarch64> = PageFlags::<Aarch64>::new();
    p.user(true);
    p.bits()
}

fn page_size_x86() -> u64 { X86_64::PAGE_SIZE }
fn page_size_arm() -> u64 { Aarch64::PAGE_SIZE }
fn page_mask_arm() -> u64 { Aarch64::PAGE_MASK }
fn levels_x86() -> u64 { X86_64::PAGE_LEVELS }
fn levels_arm() -> u64 { Aarch64::PAGE_LEVELS }

/* The frame bitmap, exposed as a slice built with `core::slice::from_raw_parts`
 * -- upstream reaches for that constantly, and it is exactly Crust's own
 * slice, so it needs no conversion. */
fn bitmap_popcount(k: *mut Kernel) -> i32 {
    let words: &[i64] = core::slice::from_raw_parts(&k.frames.words[0], 8);
    let mut bits: i32 = 0;
    for w in words {
        let mut v: i64 = w;
        while v != 0 {
            bits += (v & 1) as i32;
            v = (v >> 1) & 0x7FFFFFFFFFFFFFFF;
        }
    }
    bits
}

/* `cmp::min` and the pointer helpers, in the shape upstream writes them. */
fn budget(k: *mut Kernel, want: i32) -> i32 {
    core::cmp::min(want, k.frames.free as i32)
}

fn zero_ticks(k: *mut Kernel, pid: i32) {
    let slot: i32 = pid - 1;
    if slot >= 0 && slot < k.used {
        core::ptr::write(&k.ctx[slot].ticks, 0);
    }
}

fn heap_frame() -> u64 {
    // `kernel_heap_offset` is upstream Redox, compiled by Crust and linked
    // from vendor/kernel/src/arch/x86/consts.rs.
    PageFlags::<X86_64>::frame_of(kernel_heap_offset())
}

/* ------------------------------------------------------------------ */
/* Formatting, in the shape upstream writes it                          */
/*                                                                      */
/* Redox has 190 `write!`/`writeln!` calls and formats its types through */
/* `impl fmt::Debug for X { fn fmt(&self, f) }`. The same shape works    */
/* here, over a bounded formatter whose storage the caller supplies -- a */
/* kernel log wants truncation, not allocation.                          */
/* ------------------------------------------------------------------ */

impl fmt::Debug for Context {
    fn fmt(&self, f: *mut Formatter) -> i32 {
        write!(f, "Context {{ pid: {}, prio: {}, ticks: {}, frames: {} }}",
               self.pid, self.prio, self.ticks, self.frames)
    }
}

impl fmt::Debug for Frames {
    fn fmt(&self, f: *mut Formatter) -> i32 {
        write!(f, "Frames {{ free: {}, used: {} }}",
               self.free, FRAMES - self.free)
    }
}

/* A kernel log line: the whole state of one context, rendered into a
 * caller-owned buffer and returned as a C string. */
fn log_context(k: *mut Kernel, pid: i32, buf: *mut c_char, cap: i64) -> *mut c_char {
    let mut f: Formatter = Formatter::new(buf, cap);
    let slot: i32 = pid - 1;
    if slot < 0 || slot >= k.used {
        write!(f, "<no such pid {}>", pid);
        return f.as_str();
    }
    write!(f, "[{}] ", pid);
    k.ctx[slot].fmt(&f);
    f.as_str()
}

fn log_frames(k: *mut Kernel, buf: *mut c_char, cap: i64) -> *mut c_char {
    let mut f: Formatter = Formatter::new(buf, cap);
    k.frames.fmt(&f);
    f.as_str()
}

int main(void) {
    /* rpython module globals -- the scheme name table -- must be built
     * before anything reads them. */
    schemes_init();

    Kernel k;
    Kernel_init(&k);

    printf("CrustOS\n");
    printf("  schemes      : %d\n", scheme_count());
    printf("  heap offset  : 0x%lx  (from vendor/kernel arch consts)\n",
           kernel_heap_offset());

    int init = Kernel_spawn(&k, 0);
    int shell = Kernel_spawn(&k, 1);
    int idle = Kernel_spawn(&k, 3);

    char *urls = "sys:/context,memory:/free,bogus:/x,file:/etc/rc,irq:/1";
    printf("  opened       : %d of 5 URLs\n", open_batch(&k, urls, init));
    printf("  first fd     : %s\n", describe(scheme_of("sys:/context"),
                                             "sys:/context"));

    Kernel_dispatch(&k, (Call){ .tag = Call_Alloc,
                                .u.Alloc = { ._0 = init, ._1 = 12 } });
    Kernel_dispatch(&k, (Call){ .tag = Call_Alloc,
                                .u.Alloc = { ._0 = shell, ._1 = 5 } });
    printf("  frames       : %ld free of %ld\n", k.frames.free, FRAMES);

    Kernel_block(&k, idle);
    printf("  runnable     : %d of %d\n", Kernel_runnable(&k), k.used);
    printf("  switches     : %ld\n", Kernel_schedule(&k, 4));
    printf("  ticks        : init=%ld shell=%ld idle=%ld\n",
           k.ctx[0].ticks, k.ctx[1].ticks, k.ctx[2].ticks);

    /* Page flags in upstream's arch-generic shape: one implementation, two
     * architectures, resolved at compile time with no dispatch. */
    printf("  x86_64 kpage : 0x%lx present=%d\n",
           kernel_page_bits(), kernel_page_present());
    printf("  arm64 upage  : 0x%lx\n", user_page_bits_arm64());
    printf("  heap frame   : %lu\n", heap_frame());
    /* Inherited trait consts: PAGE_SIZE is derived from each arch's own
     * PAGE_SHIFT, and PAGE_LEVELS is a trait default arm64 overrides. */
    printf("  page size    : x86=%lu arm64=%lu (mask 0x%lx)\n",
           page_size_x86(), page_size_arm(), page_mask_arm());
    printf("  page levels  : x86=%lu arm64=%lu\n",
           levels_x86(), levels_arm());
    /* core intrinsics: a slice over the bitmap, and a clamped request. */
    printf("  frames used  : %d (bitmap popcount)\n", bitmap_popcount(&k));
    printf("  budget(1000) : %d\n", budget(&k, 1000));
    zero_ticks(&k, init);
    printf("  init ticks   : %ld (after ptr::write)\n", k.ctx[0].ticks);

    /* Formatted kernel log lines, through `impl fmt::Debug`. */
    char line[128];
    printf("  %s\n", log_context(&k, init, line, 128));
    printf("  %s\n", log_context(&k, shell, line, 128));
    printf("  %s\n", log_frames(&k, line, 128));
    printf("  %s\n", log_context(&k, 99, line, 128));

    printf("  read(0,64)   : %d\n", Kernel_dispatch(&k, (Call){
        .tag = Call_Read, .u.Read = { ._0 = 0, ._1 = 64 } }));
    printf("  close(0)     : %d\n", Kernel_dispatch(&k, (Call){
        .tag = Call_Close, .u.Close = { ._0 = 0 } }));
    printf("  bad fd       : %d\n", Kernel_dispatch(&k, (Call){
        .tag = Call_Read, .u.Read = { ._0 = 99, ._1 = 1 } }));
    return 0;
}
