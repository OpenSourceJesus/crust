/* crust_memsafe -- see crust_memsafe.h for the model.
 *
 * Layout of the state, all static so the runtime never allocates through the
 * allocator it is watching:
 *
 *   regions[]   one record per object ever registered, live or retired.
 *               Retired records are kept (quarantined) so a use-after-free
 *               still finds the object and can name where it was freed.
 *   buckets[]   page number -> chain of region indices, for O(1)-ish lookup
 *               of the object containing an arbitrary interior pointer.
 *   bigs[]      regions spanning more pages than it is worth chaining;
 *               scanned linearly, which is fine because there are few.
 *   bitmaps[]   one bit per byte of every tracked object, set when the byte
 *               is written, tested when it is read. This is what makes the
 *               uninitialized-read report say *which* bytes were garbage.
 */

#include "crust_memsafe.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Crust's own freestanding <stdlib.h> declares atexit but not exit/_Exit;
 * glibc declares both. Redeclaring with compatible types satisfies the first
 * without upsetting the second, so one source builds under gcc and under
 * shivycx. */
void exit(int);
void _Exit(int);

/* Under shivycx the link line has no crtbegin, so glibc's atexit pulls in a
 * `__dso_handle` nobody defines. Supplying it here keeps the runtime linkable
 * by the compiler it ships with; under gcc crtbegin already provides it, so
 * the definition is confined to the shivycx build. */
#ifdef __SHIVYC__
void *__dso_handle = 0;
#endif

#define MS_LIVE    0
#define MS_FREED   1   /* went through free() */
#define MS_POPPED  2   /* stack frame left scope */

#define MS_BIG_PAGES 64        /* chain up to this many pages, else bigs[] */
#define MS_NEAR_SLACK 4096     /* how far past an object we still name it */
#define MS_MAX_DEDUP 4096

typedef unsigned long ms_addr;

struct ms_region {
    ms_addr base;
    size_t size;
    long id;
    int kind;
    int state;
    long bitmap;              /* byte offset into bitmaps[], -1 = untracked */
    const char *what;
    const char *afile;        /* allocation site */
    int aline;
    const char *afunc;
    const char *ffile;        /* retirement site */
    int fline;
    const char *ffunc;
    int next;                 /* bucket chain */
};

static struct ms_region ms_regions[CRUST_MS_MAX_REGIONS];
static int ms_bucket[CRUST_MS_HASH_SLOTS];
static int ms_bigs[1024];
static unsigned char ms_bitmaps[CRUST_MS_BITMAP_ARENA];

static int ms_nregions;
static int ms_nbigs;
static long ms_bitmap_used;
static long ms_next_id = 1;
static int ms_ready;
static int ms_halt = 0;
static int ms_leaks = 1;
static int ms_fail_exit = 1;
static long ms_errors;
static long ms_bitmap_exhausted;

/* Error dedup: a site that fires inside a loop should be reported once with
 * a count, not ten thousand times. */
struct ms_seen { const char *file; int line; int kind; long count; };
static struct ms_seen ms_dedup[MS_MAX_DEDUP];
static int ms_ndedup;

/* ---------------------------------------------------------------------- */

static unsigned long ms_pageno(ms_addr a)
{
    return (unsigned long)(a >> CRUST_MS_PAGE_SHIFT);
}

static unsigned long ms_slot(unsigned long page)
{
    /* Fibonacci hashing: pages from one mmap arrive consecutively, and the
     * low bits alone would pile them into neighbouring buckets. */
    unsigned long h = page * 11400714819323198485UL;
    return (h >> 40) & (unsigned long)(CRUST_MS_HASH_SLOTS - 1);
}

void crust_ms_set_halt_on_error(int on) { ms_halt = on; }
void crust_ms_set_report_leaks(int on) { ms_leaks = on; }
void crust_ms_set_fail_exit(int on) { ms_fail_exit = on; }

static void ms_atexit(void)
{
    int rc = crust_ms_report();
    /* A test run that found bugs has to *fail*, or `make test` stays green
     * while the report scrolls past. Overriding the process status from an
     * atexit handler needs _Exit: calling exit() again from here is
     * undefined, and _Exit skips the remaining handlers by design. */
    if (rc && ms_fail_exit) {
        fflush(stdout);
        fflush(stderr);
        _Exit(1);
    }
}

void crust_ms_init(void)
{
    int i;
    if (ms_ready) return;
    ms_ready = 1;
    for (i = 0; i < CRUST_MS_HASH_SLOTS; i++) ms_bucket[i] = -1;
    atexit(ms_atexit);
}

/* ---------------------------------------------------------------------- */
/* bucket maintenance                                                      */

static void ms_link(int idx)
{
    struct ms_region *r = &ms_regions[idx];
    unsigned long p0 = ms_pageno(r->base);
    unsigned long p1 = ms_pageno(r->base + (r->size ? r->size - 1 : 0));
    unsigned long s;

    /* Objects wider than the lookup's walk-back window go on the linear
     * list; everything else is chained from its *first* page only, which is
     * what ms_find's walk backwards from the accessed page expects. Chaining
     * from every touched page would need a per-page link, and the walk-back
     * makes it unnecessary. */
    if (p1 - p0 >= MS_BIG_PAGES) {
        if (ms_nbigs < (int)(sizeof(ms_bigs) / sizeof(ms_bigs[0])))
            ms_bigs[ms_nbigs++] = idx;
        else
            r->next = -1;               /* list full: bounds-checked only */
        return;
    }
    s = ms_slot(p0);
    r->next = ms_bucket[s];
    ms_bucket[s] = idx;
}

/* Last region returned by ms_find. Accesses cluster overwhelmingly: a loop
 * walks one array, a function works on one object. Once the compiler has
 * proved bounds and liveness away, what remains at each write is the
 * definedness update, and its region lookup became the dominant cost -- so the
 * hit rate here is what decides the overhead of a fully-proved loop.
 *
 * Cleared whenever a record could be reused or retired, so a stale pointer
 * cannot outlive what it describes. */
static struct ms_region *ms_cache;

static void ms_cache_flush(void)
{
    ms_cache = 0;
}

static struct ms_region *ms_find(ms_addr a)
{
    unsigned long page;

    if (ms_cache && a >= ms_cache->base
            && a < ms_cache->base + ms_cache->size)
        return ms_cache;

    page = ms_pageno(a);
    int i;
    unsigned long back;

    /* the accessed page, then a few pages back, so a pointer into the middle
     * of a multi-page object still resolves */
    for (back = 0; back < MS_BIG_PAGES; back++) {
        int idx;
        if (back > page) break;
        idx = ms_bucket[ms_slot(page - back)];
        while (idx != -1) {
            struct ms_region *r = &ms_regions[idx];
            if (a >= r->base && a < r->base + r->size) {
                ms_cache = r;
                return r;
            }
            idx = r->next;
        }
    }
    for (i = 0; i < ms_nbigs; i++) {
        struct ms_region *r = &ms_regions[ms_bigs[i]];
        if (a >= r->base && a < r->base + r->size) {
            ms_cache = r;
            return r;
        }
    }
    return 0;
}

/* The object an out-of-range address most plausibly belongs to: the region
 * ending closest below it, or -- for an underflow -- the one starting closest
 * above it. `*off` is set to the signed offset from that object's base, so a
 * negative value means the access is before the start.
 *
 * Looking upward matters more than it sounds. `a[-2]` is not merely rarer than
 * `a[n]`; it is invisible without this, because the address lands in no
 * tracked region at all and the "unknown address is not an error" rule then
 * lets it through. It corrupts the allocator's own metadata and the program
 * dies later inside free() with no location. */
static struct ms_region *ms_find_near(ms_addr a, long *off)
{
    struct ms_region *best = 0;
    ms_addr bestend = 0;
    struct ms_region *above = 0;
    ms_addr abovebase = 0;
    unsigned long page = ms_pageno(a);
    unsigned long back;
    int i;

    for (back = 0; back < MS_BIG_PAGES + 1; back++) {
        int idx;
        /* back == 0 is the page above, so a region beginning just after the
         * address is found too; the rest walk backwards as before. */
        unsigned long pg = back == 0 ? page + 1 : page - (back - 1);
        if (back > page + 1) break;
        idx = ms_bucket[ms_slot(pg)];
        while (idx != -1) {
            struct ms_region *r = &ms_regions[idx];
            ms_addr end = r->base + r->size;
            if (end <= a && a - end < MS_NEAR_SLACK
                    && (end > bestend
                        || (end == bestend && r->state == MS_LIVE))) {
                best = r; bestend = end;
            }
            if (r->base > a && r->base - a < MS_NEAR_SLACK
                    && (!above || r->base < abovebase
                        || (r->base == abovebase && r->state == MS_LIVE))) {
                above = r; abovebase = r->base;
            }
            idx = r->next;
        }
    }
    for (i = 0; i < ms_nbigs; i++) {
        struct ms_region *r = &ms_regions[ms_bigs[i]];
        ms_addr end = r->base + r->size;
        if (end <= a && a - end < MS_NEAR_SLACK
                && (end > bestend
                    || (end == bestend && r->state == MS_LIVE))) {
            best = r; bestend = end;
        }
        if (r->base > a && r->base - a < MS_NEAR_SLACK
                && (!above || r->base < abovebase
                    || (r->base == abovebase && r->state == MS_LIVE))) {
            above = r; abovebase = r->base;
        }
    }
    if (!best && above) {
        *off = -(long)(above->base - a);
        return above;
    }
    if (best) *off = (long)(a - best->base);
    return best;
}

/* ---------------------------------------------------------------------- */
/* init-shadow bits                                                         */

static void ms_bits_set(struct ms_region *r, ms_addr a, size_t n, int on)
{
    size_t off, i;
    unsigned char *bm;
    if (!r || r->bitmap < 0) return;
    bm = &ms_bitmaps[r->bitmap];
    off = (size_t)(a - r->base);
    for (i = 0; i < n && off + i < r->size; i++) {
        size_t b = off + i;
        if (on) bm[b >> 3] |= (unsigned char)(1u << (b & 7));
        else    bm[b >> 3] &= (unsigned char)~(1u << (b & 7));
    }
}

/* Returns the offset of the first undefined byte in [a, a+n), or -1. */
static long ms_bits_first_unset(struct ms_region *r, ms_addr a, size_t n)
{
    size_t off, i;
    unsigned char *bm;
    if (!r || r->bitmap < 0) return -1;
    bm = &ms_bitmaps[r->bitmap];
    off = (size_t)(a - r->base);
    for (i = 0; i < n && off + i < r->size; i++) {
        size_t b = off + i;
        if (!(bm[b >> 3] & (unsigned char)(1u << (b & 7)))) return (long)i;
    }
    return -1;
}

/* ---------------------------------------------------------------------- */
/* diagnostics                                                              */

static const char *ms_bound_title(int kind, int under)
{
    if (kind == CRUST_MS_STACK)
        return under ? "stack buffer underflow" : "stack buffer overflow";
    if (kind == CRUST_MS_GLOBAL)
        return under ? "global buffer underflow" : "global buffer overflow";
    return under ? "heap buffer underflow" : "heap buffer overflow";
}


static const char *ms_kindname(int kind)
{
    if (kind == CRUST_MS_HEAP) return "heap";
    if (kind == CRUST_MS_STACK) return "stack";
    return "global";
}

static int ms_same_file(const char *a, const char *b)
{
    /* Compared by content, not by pointer. Two tiers can report the same site
     * through different string literals -- a macro check in the source and a
     * compiler-inserted check both naming the same file -- and pointer
     * equality would then see two distinct sites and print the same bug twice.
     */
    if (a == b) return 1;
    if (!a || !b) return 0;
    return strcmp(a, b) == 0;
}


static int ms_dedup_hit(const char *file, int line, int kind)
{
    int i;
    for (i = 0; i < ms_ndedup; i++) {
        if (ms_dedup[i].line == line && ms_dedup[i].kind == kind
                && ms_same_file(ms_dedup[i].file, file)) {
            ms_dedup[i].count++;
            return ms_dedup[i].count > 1;
        }
    }
    if (ms_ndedup < MS_MAX_DEDUP) {
        ms_dedup[ms_ndedup].file = file;
        ms_dedup[ms_ndedup].line = line;
        ms_dedup[ms_ndedup].kind = kind;
        ms_dedup[ms_ndedup].count = 1;
        ms_ndedup++;
    }
    return 0;
}

static void ms_where(const char *file, int line, const char *func)
{
    fprintf(stderr, "  at %s:%d", file ? file : "?", line);
    if (func) fprintf(stderr, " in %s", func);
    fprintf(stderr, "\n");
}

static void ms_describe(struct ms_region *r)
{
    fprintf(stderr, "    object: %lu bytes", (unsigned long)r->size);
    if (r->what) fprintf(stderr, ", %s", r->what);
    fprintf(stderr, " (%s)\n", ms_kindname(r->kind));
    fprintf(stderr, "            spans 0x%lx .. 0x%lx\n",
            (unsigned long)r->base, (unsigned long)(r->base + r->size));
    if (r->afile) {
        /* A frame object or a global is declared, not allocated; saying
         * "allocated" of `int buf[8]` sends the reader looking for a malloc
         * that does not exist. */
        fprintf(stderr, "            %s at %s:%d",
                r->kind == CRUST_MS_HEAP ? "allocated" : "declared",
                r->afile, r->aline);
        if (r->afunc) fprintf(stderr, " in %s", r->afunc);
        fprintf(stderr, "\n");
    }
    if (r->state != MS_LIVE && r->ffile) {
        fprintf(stderr, "            %s at %s:%d",
                r->state == MS_FREED ? "freed" : "left scope",
                r->ffile, r->fline);
        if (r->ffunc) fprintf(stderr, " in %s", r->ffunc);
        fprintf(stderr, "\n");
    }
}

static void ms_head(const char *title, const char *expr,
                    const char *file, int line, const char *func)
{
    ms_errors++;
    fprintf(stderr, "\ncrust --mem-safe: %s\n", title);
    ms_where(file, line, func);
    if (expr) fprintf(stderr, "    while evaluating `%s`\n", expr);
}

static void ms_maybe_halt(void)
{
    if (ms_halt) {
        fprintf(stderr, "\n--mem-safe: halting on first error "
                        "(crust_ms_set_halt_on_error)\n");
        exit(1);
    }
}

/* ---------------------------------------------------------------------- */
/* registration                                                             */

void crust_ms_register(void *base, size_t size, int kind, int initialized,
                       const char *what, const char *file, int line,
                       const char *func)
{
    struct ms_region *r;
    int idx;

    if (!ms_ready) crust_ms_init();
    if (!base || size == 0) return;
    ms_cache_flush();

    if (ms_nregions >= CRUST_MS_MAX_REGIONS) {
        /* Reuse the oldest retired slot; live objects are never evicted, so
         * running out means the quarantine is full, not that tracking stops. */
        int i, victim = -1;
        for (i = 0; i < ms_nregions; i++) {
            if (ms_regions[i].state != MS_LIVE) { victim = i; break; }
        }
        if (victim < 0) return;         /* genuinely out of room: stop tracking */
        idx = victim;
    } else {
        idx = ms_nregions++;
    }

    r = &ms_regions[idx];
    r->base = (ms_addr)base;
    r->size = size;
    r->id = ms_next_id++;
    r->kind = kind;
    r->state = MS_LIVE;
    r->what = what;
    r->afile = file; r->aline = line; r->afunc = func;
    r->ffile = 0; r->fline = 0; r->ffunc = 0;
    r->next = -1;

    {
        long need = (long)((size + 7) / 8);
        if (ms_bitmap_used + need <= CRUST_MS_BITMAP_ARENA) {
            r->bitmap = ms_bitmap_used;
            ms_bitmap_used += need;
            memset(&ms_bitmaps[r->bitmap], initialized ? 0xff : 0x00,
                   (size_t)need);
        } else {
            r->bitmap = -1;             /* bounds still checked, init is not */
            ms_bitmap_exhausted++;
        }
    }
    ms_link(idx);
}

void crust_ms_unregister(void *base, const char *file, int line,
                         const char *func)
{
    struct ms_region *r;
    if (!ms_ready) crust_ms_init();
    if (!base) return;
    r = ms_find((ms_addr)base);
    if (!r || r->state != MS_LIVE) return;
    r->state = MS_POPPED;
    r->ffile = file; r->fline = line; r->ffunc = func;
    ms_cache_flush();
}

/* ---------------------------------------------------------------------- */
/* the checks                                                               */

static int ms_check(const void *p, size_t n, int writing, const char *expr,
                    const char *file, int line, const char *func)
{
    ms_addr a = (ms_addr)p;
    struct ms_region *r;
    const char *verb = writing ? "write" : "read";

    if (!ms_ready) crust_ms_init();

    if (!p) {
        if (ms_dedup_hit(file, line, 1)) return 0;
        ms_head("null pointer dereference", expr, file, line, func);
        fprintf(stderr, "    %s of %lu byte%s through a null pointer\n",
                verb, (unsigned long)n, n == 1 ? "" : "s");
        ms_maybe_halt();
        return 0;
    }

    r = ms_find(a);
    if (!r) {
        /* An address in no tracked object. Under partial instrumentation this
         * is usually memory owned by uninstrumented C, so it is only an error
         * when it sits just past something we do know -- which is exactly the
         * far-overrun case (`buf[i]` with i wildly too large). */
        long off = 0;
        struct ms_region *near = ms_find_near(a, &off);
        if (!near) return 1;
        if (ms_dedup_hit(file, line, 2)) return 0;
        ms_head(ms_bound_title(near->kind, off < 0), expr, file, line, func);
        fprintf(stderr, "    %s of %lu byte%s at 0x%lx\n",
                verb, (unsigned long)n, n == 1 ? "" : "s", (unsigned long)a);
        ms_describe(near);
        if (off < 0) {
            fprintf(stderr, "    offset %ld into a %lu-byte object: the %s "
                            "begins %lu byte%s before the start\n",
                    off, (unsigned long)near->size, verb,
                    (unsigned long)(-off), off == -1 ? "" : "s");
        } else {
            unsigned long past = (unsigned long)(a - (near->base + near->size));
            if (past == 0)
                fprintf(stderr, "    offset %ld into a %lu-byte object: the "
                                "%s begins at the first byte past the end\n",
                        off, (unsigned long)near->size, verb);
            else
                fprintf(stderr, "    offset %ld into a %lu-byte object: the "
                                "%s begins %lu byte%s past the end\n",
                        off, (unsigned long)near->size, verb, past,
                        past == 1 ? "" : "s");
        }
        ms_maybe_halt();
        return 0;
    }

    if (r->state != MS_LIVE) {
        if (ms_dedup_hit(file, line, 3)) return 0;
        ms_head(r->state == MS_FREED ? "use after free"
                                     : "use after scope exit",
                expr, file, line, func);
        fprintf(stderr, "    %s of %lu byte%s at 0x%lx, offset %lu into the "
                        "object\n",
                verb, (unsigned long)n, n == 1 ? "" : "s", (unsigned long)a,
                (unsigned long)(a - r->base));
        ms_describe(r);
        ms_maybe_halt();
        return 0;
    }

    if (a + n > r->base + r->size) {
        unsigned long over = (unsigned long)((a + n) - (r->base + r->size));
        if (ms_dedup_hit(file, line, 4)) return 0;
        ms_head(ms_bound_title(r->kind, 0), expr, file, line, func);
        fprintf(stderr, "    %s of %lu byte%s at 0x%lx\n",
                verb, (unsigned long)n, n == 1 ? "" : "s", (unsigned long)a);
        ms_describe(r);
        fprintf(stderr, "    offset %lu into a %lu-byte object: the %s runs "
                        "%lu byte%s past the end\n",
                (unsigned long)(a - r->base), (unsigned long)r->size, verb,
                over, over == 1 ? "" : "s");
        ms_maybe_halt();
        return 0;
    }

    if (writing) {
        ms_bits_set(r, a, n, 1);
        return 1;
    }

    {
        long bad = ms_bits_first_unset(r, a, n);
        if (bad >= 0) {
            if (ms_dedup_hit(file, line, 5)) return 0;
            ms_head("read of uninitialized memory", expr, file, line, func);
            fprintf(stderr, "    read of %lu bytes at 0x%lx, offset %lu into "
                            "the object\n",
                    (unsigned long)n, (unsigned long)a,
                    (unsigned long)(a - r->base));
            fprintf(stderr, "    byte %ld of the read was never written\n", bad);
            ms_describe(r);
            ms_maybe_halt();
            return 0;
        }
    }
    return 1;
}

int crust_ms_check_read(const void *p, size_t n, const char *expr,
                        const char *file, int line, const char *func)
{
    return ms_check(p, n, 0, expr, file, line, func);
}

int crust_ms_check_write(void *p, size_t n, const char *expr,
                         const char *file, int line, const char *func)
{
    return ms_check(p, n, 1, expr, file, line, func);
}

void crust_ms_stack(void *base, unsigned long size, int initialized,
                    const char *name, const char *file, int line)
{
    crust_ms_register(base, (size_t)size, CRUST_MS_STACK, initialized,
                      name, file, line, 0);
}

void crust_ms_stack_end(void *base, const char *file, int line)
{
    crust_ms_unregister(base, file, line, 0);
}

void crust_ms_global(void *base, unsigned long size, const char *name,
                     const char *file, int line)
{
    struct ms_region *r;
    if (!ms_ready) crust_ms_init();
    if (!base || size == 0) return;
    r = ms_find((ms_addr)base);
    if (r && r->base == (ms_addr)base && r->state == MS_LIVE)
        return;                     /* already known; a repeat, not a new object */
    crust_ms_register(base, (size_t)size, CRUST_MS_GLOBAL, 1,
                      name, file, line, 0);
}

void crust_ms_il_read(const void *p, unsigned long n, const char *file,
                      int line, const char *func)
{
    ms_check(p, (size_t)n, 0, 0, file, line, func);
}

void crust_ms_il_write(void *p, unsigned long n, const char *file,
                       int line, const char *func)
{
    ms_check(p, (size_t)n, 1, 0, file, line, func);
}

void crust_ms_il_escape(void *p)
{
    struct ms_region *r;
    ms_addr a = (ms_addr)p;
    if (!ms_ready) crust_ms_init();
    if (!p) return;
    r = ms_find(a);
    if (!r || r->state != MS_LIVE) return;
    /* From the pointer forward, not the whole object: `strcpy(buf + 8, ..)`
     * says nothing about the first eight bytes, and keeping them poisoned
     * preserves detection for the common partial-fill case. */
    ms_bits_set(r, a, r->size - (size_t)(a - r->base), 1);
}

void *crust_ms_checked_wr(void *p, size_t n, const char *expr,
                          const char *file, int line, const char *func)
{
    ms_check(p, n, 1, expr, file, line, func);
    return p;
}

void crust_ms_mark_init(void *p, size_t n)
{
    struct ms_region *r;
    if (!ms_ready) crust_ms_init();
    if (!p) return;
    r = ms_find((ms_addr)p);
    if (r && r->state == MS_LIVE) ms_bits_set(r, (ms_addr)p, n, 1);
}

/* ---------------------------------------------------------------------- */
/* allocators                                                               */

void *crust_ms_malloc(size_t n, const char *file, int line, const char *func)
{
    void *p;
    if (!ms_ready) crust_ms_init();
    p = malloc(n);
    if (p) crust_ms_register(p, n, CRUST_MS_HEAP, 0, "malloc", file, line, func);
    return p;
}

void *crust_ms_calloc(size_t nm, size_t n, const char *file, int line,
                      const char *func)
{
    void *p;
    if (!ms_ready) crust_ms_init();
    p = calloc(nm, n);
    if (p) crust_ms_register(p, nm * n, CRUST_MS_HEAP, 1, "calloc", file,
                             line, func);
    return p;
}

void crust_ms_free(void *p, const char *file, int line, const char *func)
{
    struct ms_region *r;
    if (!ms_ready) crust_ms_init();
    if (!p) return;                     /* free(NULL) is well defined */

    r = ms_find((ms_addr)p);
    if (r && r->state == MS_FREED && r->base == (ms_addr)p) {
        ms_head("double free", 0, file, line, func);
        fprintf(stderr, "    free of 0x%lx, which was already freed\n",
                (unsigned long)p);
        ms_describe(r);
        ms_maybe_halt();
        return;                         /* do not hand it to libc twice */
    }
    if (r && r->state == MS_LIVE && r->base != (ms_addr)p) {
        ms_head("free of an interior pointer", 0, file, line, func);
        fprintf(stderr, "    free of 0x%lx, which is %lu bytes into an "
                        "object rather than its base\n",
                (unsigned long)p, (unsigned long)((ms_addr)p - r->base));
        ms_describe(r);
        ms_maybe_halt();
        return;
    }
    if (r && r->kind != CRUST_MS_HEAP) {
        ms_head("free of non-heap memory", 0, file, line, func);
        fprintf(stderr, "    free of 0x%lx\n", (unsigned long)p);
        ms_describe(r);
        ms_maybe_halt();
        return;
    }

    if (r) {
        r->state = MS_FREED;
        r->ffile = file; r->fline = line; r->ffunc = func;
        ms_cache_flush();
        /* The bytes are gone; clearing the shadow means a later read through
         * a stale alias reports use-after-free rather than reading bits that
         * happen to still look defined. */
        ms_bits_set(r, r->base, r->size, 0);
    }
    free(p);
}

void *crust_ms_realloc(void *p, size_t n, const char *file, int line,
                       const char *func)
{
    struct ms_region *r;
    void *q;
    size_t old = 0;

    if (!ms_ready) crust_ms_init();
    if (!p) return crust_ms_malloc(n, file, line, func);

    r = ms_find((ms_addr)p);
    if (r && r->state != MS_LIVE) {
        ms_head("realloc of freed memory", 0, file, line, func);
        fprintf(stderr, "    realloc of 0x%lx\n", (unsigned long)p);
        ms_describe(r);
        ms_maybe_halt();
        return 0;
    }
    if (r) { old = r->size; r->state = MS_FREED; r->ffile = file;
             r->fline = line; r->ffunc = func; ms_cache_flush(); }

    q = realloc(p, n);
    if (!q) return 0;
    crust_ms_register(q, n, CRUST_MS_HEAP, 0, "realloc", file, line, func);
    /* The copied prefix keeps whatever definedness it had; only the growth
     * is fresh garbage. Approximate by marking the carried-over bytes
     * defined, which is what realloc actually guarantees. */
    if (old) crust_ms_mark_init(q, old < n ? old : n);
    return q;
}

/* ---------------------------------------------------------------------- */
/* summary                                                                  */

int crust_ms_report(void)
{
    int i;
    long leaked = 0, leakbytes = 0;
    static int done;

    if (done) return ms_errors ? 1 : 0;
    done = 1;

    if (ms_leaks) {
        for (i = 0; i < ms_nregions; i++) {
            struct ms_region *r = &ms_regions[i];
            if (r->state == MS_LIVE && r->kind == CRUST_MS_HEAP) {
                leaked++;
                leakbytes += (long)r->size;
            }
        }
    }

    if (ms_errors == 0 && leaked == 0) {
        fprintf(stderr, "\ncrust --mem-safe: clean (%d object%s tracked)\n",
                ms_nregions, ms_nregions == 1 ? "" : "s");
        return 0;
    }

    if (leaked) {
        fprintf(stderr, "\ncrust --mem-safe: %ld heap object%s still live at "
                        "exit (%ld bytes)\n",
                leaked, leaked == 1 ? "" : "s", leakbytes);
        for (i = 0; i < ms_nregions; i++) {
            struct ms_region *r = &ms_regions[i];
            if (r->state == MS_LIVE && r->kind == CRUST_MS_HEAP) {
                fprintf(stderr, "    %lu bytes", (unsigned long)r->size);
                if (r->afile) {
                    fprintf(stderr, " allocated at %s:%d", r->afile, r->aline);
                    if (r->afunc) fprintf(stderr, " in %s", r->afunc);
                }
                fprintf(stderr, "\n");
            }
        }
    }

    /* Repeat counts for sites that fired more than once, so a loop reads as
     * one bug with a multiplicity rather than a wall of output. */
    for (i = 0; i < ms_ndedup; i++) {
        if (ms_dedup[i].count > 1) {
            fprintf(stderr, "    (%s:%d reported %ld more time%s)\n",
                    ms_dedup[i].file ? ms_dedup[i].file : "?",
                    ms_dedup[i].line, ms_dedup[i].count - 1,
                    ms_dedup[i].count == 2 ? "" : "s");
        }
    }

    if (ms_bitmap_exhausted) {
        fprintf(stderr, "    (note: the init-shadow arena filled; %ld object%s "
                        "had bounds checked but not definedness -- raise "
                        "CRUST_MS_BITMAP_ARENA)\n",
                ms_bitmap_exhausted, ms_bitmap_exhausted == 1 ? "" : "s");
    }

    fprintf(stderr, "\ncrust --mem-safe: %ld error%s\n",
            ms_errors, ms_errors == 1 ? "" : "s");
    return ms_errors ? 1 : 0;
}
