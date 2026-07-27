/* small_os -- a Redox-shaped kernel sketch in three languages.
 *
 *   schemes.py  rpython -- URL routing, path parsing, listings. Text and
 *                          list work, where rpython is shortest and Crust
 *                          would need a string type and an iterator protocol
 *                          it does not have.
 *   this file   Rust    -- the context table, the frame allocator's bitmap,
 *                          the scheduler loop and syscall dispatch. Fixed
 *                          layouts, bit twiddling, tight loops: no allocator
 *                          and no boxing between the code and the machine.
 *               C       -- boot and reporting.
 *
 * The borrowed idea is Redox's: every resource is named by a URL and the
 * kernel routes an open to whichever scheme claims the prefix. What is not
 * borrowed is the size -- this is the whole shape in about 200 lines, because
 * each half is written in the language that makes it short.
 */
#include "schemes.py"

int printf(const char *, ...);

const MAX_CONTEXTS: usize = 16;
const MAX_FILES: usize = 8;
const FRAME_WORDS: usize = 4;      /* 4 * 64 = 256 frames */
const FRAMES: i64 = 256;

/* ------------------------------------------------------------------ */
/* Contexts                                                            */
/* ------------------------------------------------------------------ */

enum State {
    Free,
    Runnable,
    Blocked,
    Exited,
}

#[derive(Clone, Copy, Default)]
struct Context {
    pid: i32,
    prio: i32,
    ticks: i64,
    frames: i32,
}

struct Table {
    ctx: [Context; 16],
    state: [i32; 16],
    used: i32,
}

impl Table {
    // Zeroed in place rather than built as a literal: a compound literal
    // cannot brace-initialise an array-of-struct field, and looping is
    // clearer than fighting that.
    fn init(&mut self) {
        self.used = 0;
        for i in 0..16 {
            self.state[i] = 0;
            self.ctx[i].pid = 0;
            self.ctx[i].prio = 0;
            self.ctx[i].ticks = 0;
            self.ctx[i].frames = 0;
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
        self.ctx[slot].ticks = 0;
        self.ctx[slot].frames = 0;
        self.state[slot] = 1;              /* Runnable */
        self.ctx[slot].pid
    }

    fn block(&mut self, pid: i32) {
        let i: i32 = pid - 1;
        if i >= 0 && i < self.used {
            self.state[i] = 2;
        }
    }

    fn runnable(&self) -> i32 {
        let mut n: i32 = 0;
        for i in 0..self.used {
            if self.state[i] == 1 {
                n += 1;
            }
        }
        n
    }

    fn quantum(&self, i: i32) -> i64 {
        (4 - self.ctx[i].prio) as i64
    }
}

/* ------------------------------------------------------------------ */
/* Physical frame allocator -- a bitmap, one bit per frame              */
/* ------------------------------------------------------------------ */

struct Frames {
    words: [i64; 4],
    free: i64,
}

impl Frames {
    fn init(&mut self) {
        self.free = FRAMES;
        for i in 0..4 {
            self.words[i] = 0;
        }
    }

    fn taken(&self, frame: i64) -> bool {
        let w: i64 = frame / 64;
        let b: i64 = frame % 64;
        (self.words[w] >> b) & 1 == 1
    }

    fn take(&mut self, frame: i64) {
        let w: i64 = frame / 64;
        let b: i64 = frame % 64;
        if (self.words[w] >> b) & 1 == 0 {
            self.words[w] |= 1 << b;
            self.free -= 1;
        }
    }

    fn release(&mut self, frame: i64) {
        let w: i64 = frame / 64;
        let b: i64 = frame % 64;
        if (self.words[w] >> b) & 1 == 1 {
            self.words[w] &= !(1 << b);
            self.free += 1;
        }
    }

    /* First-fit. Returns -1 when there is nothing left. */
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
/* File descriptors, routed by scheme                                   */
/* ------------------------------------------------------------------ */

#[derive(Clone, Copy, Default)]
struct Fd {
    scheme: i32,
    owner: i32,
    reads: i32,
}

/* ------------------------------------------------------------------ */
/* Syscalls -- a data-carrying enum, dispatched by `match`              */
/* ------------------------------------------------------------------ */

enum Call {
    Open { scheme: i32, owner: i32 },
    Read(i32, i32),
    Close(i32),
    Yield,
}

struct Kernel {
    table: Table,
    frames: Frames,
    fds: [Fd; 8],
    open_fds: i32,
}

impl Kernel {
    fn init(&mut self) {
        self.table.init();
        self.frames.init();
        self.open_fds = 0;
        for i in 0..8 {
            self.fds[i].scheme = SCHEME_NONE;
            self.fds[i].owner = 0;
            self.fds[i].reads = 0;
        }
    }

    fn dispatch(&mut self, c: Call) -> i32 {
        match c {
            Call::Open { scheme, owner } => self.do_open(scheme, owner),
            Call::Read(fd, n) => self.do_read(fd, n),
            Call::Close(fd) => self.do_close(fd),
            Call::Yield => 0,
        }
    }

    fn do_open(&mut self, scheme: i32, owner: i32) -> i32 {
        if scheme < 0 || self.open_fds >= 8 {
            return -1;                     /* ENOENT / EMFILE */
        }
        let fd: i32 = self.open_fds;
        self.open_fds += 1;
        self.fds[fd].scheme = scheme;
        self.fds[fd].owner = owner;
        self.fds[fd].reads = 0;
        fd
    }

    fn do_read(&mut self, fd: i32, n: i32) -> i32 {
        if fd < 0 || fd >= self.open_fds {
            return -1;                     /* EBADF */
        }
        self.fds[fd].reads += n;
        self.fds[fd].reads
    }

    fn do_close(&mut self, fd: i32) -> i32 {
        if fd < 0 || fd >= self.open_fds {
            return -1;
        }
        self.fds[fd].scheme = SCHEME_NONE;
        0
    }

    /* Round-robin over the runnable contexts, charging each its quantum. */
    fn schedule(&mut self, rounds: i32) -> i64 {
        let mut switches: i64 = 0;
        for _r in 0..rounds {
            for i in 0..self.table.used {
                if self.table.state[i] == 1 {
                    let q: i64 = self.table.quantum(i);
                    self.table.ctx[i].ticks += q;
                    switches += 1;
                }
            }
        }
        switches
    }
}

/* ------------------------------------------------------------------ */
/* Boot: the two halves meet here                                       */
/* ------------------------------------------------------------------ */

/* Route a batch of URLs in rpython, then open one descriptor per accepted
 * one. The routing table is walked as an ordinary `PyList<i32>` -- three
 * words py2c built and Rust reads with no conversion. */
fn open_batch(k: *mut Kernel, urls: *mut c_char, owner: i32) -> i32 {
    let kinds: *mut PyList<i32> = route_all(urls) as *mut PyList<i32>;
    let mut opened: i32 = 0;
    for kind in kinds {
        if kind != SCHEME_NONE {
            // Bound first: a struct literal directly inside an `if`
            // condition is ambiguous with the block that follows it, in
            // Crust as in Rust.
            let call: Call = Call::Open { scheme: kind, owner: owner };
            if k.dispatch(call) >= 0 {
                opened += 1;
            }
        }
    }
    opened
}

fn total_depth(urls: *mut c_char) -> i32 {
    let ds: *mut PyList<i32> = depths_all(urls) as *mut PyList<i32>;
    let mut total: i32 = 0;
    for d in ds {
        total += d;
    }
    total
}

fn claim_frames(k: *mut Kernel, pid: i32, n: i32) -> i32 {
    let mut got: i32 = 0;
    for _i in 0..n {
        if k.frames.alloc() >= 0 {
            got += 1;
        }
    }
    let slot: i32 = pid - 1;
    if slot >= 0 && slot < k.table.used {
        k.table.ctx[slot].frames += got;
    }
    got
}

int main(void) {
    /* An rpython module with module-level globals -- here the scheme name
     * table -- needs its generated initialiser run before anything reads
     * them. py2c emits one per module, named after the file. */
    schemes_init();

    Kernel k;
    Kernel_init(&k);

    /* Three contexts at different priorities. */
    int a = Table_spawn(&k.table, 0);
    int b = Table_spawn(&k.table, 1);
    int c = Table_spawn(&k.table, 3);

    /* py2c spells `str` as `char *`. On the Rust side that is `*mut c_char`,
     * not `*mut char` -- a Rust `char` is four bytes. */
    char *urls = "file:/etc/passwd,disk:/0,bogus:/x,tcp:/80,pipe:/1/2/3";

    printf("schemes    = %d\n", scheme_count());
    printf("routed     = %d of 5 accepted\n", open_batch(&k, urls, a));
    printf("path depth = %d\n", total_depth(urls));
    printf("listing    = %s\n", describe(scheme_of("tcp:/80"), "tcp:/80"));

    /* Frames: give the first context a few pages. */
    printf("frames     = %d claimed, %ld free\n",
           claim_frames(&k, a, 5), k.frames.free);

    /* Block one context, then run the scheduler. */
    Table_block(&k.table, b);
    printf("runnable   = %d of %d\n", Table_runnable(&k.table), k.table.used);
    printf("switches   = %ld\n", Kernel_schedule(&k, 3));
    printf("ticks(a,c) = %ld %ld\n", k.table.ctx[0].ticks, k.table.ctx[2].ticks);

    /* Syscalls through the enum dispatch. */
    printf("read(0,7)  = %d\n", Kernel_dispatch(&k, (Call){
        .tag = Call_Read, .u.Read = { ._0 = 0, ._1 = 7 } }));
    printf("close(0)   = %d\n", Kernel_dispatch(&k, (Call){
        .tag = Call_Close, .u.Close = { ._0 = 0 } }));
    printf("badfd      = %d\n", Kernel_dispatch(&k, (Call){
        .tag = Call_Read, .u.Read = { ._0 = 9, ._1 = 1 } }));
    return 0;
}
