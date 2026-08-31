/* crust_memsafe -- the runtime half of `--mem-safe`.
 *
 * The model is Fil-C's, minus the ABI change. Fil-C gives every pointer a
 * capability (InvisiCaps: lower bound, upper bound, allocation liveness)
 * carried alongside the pointer itself, which is why it needs the whole
 * program recompiled. Here the same three facts live in a side table keyed by
 * address, so an instrumented translation unit links against uninstrumented C
 * without an ABI break. That is what makes the tiering work: a program can be
 * checked at the C++ subset layer only (`cpprust.py --mem-safe`) while the
 * hand-written C it calls stays untouched and full speed.
 *
 * The cost of the side table is a lookup per access instead of Fil-C's
 * register compare. That is the right trade for a flag whose whole purpose is
 * to be on under `make test` and off in the release build.
 *
 * What is detected:
 *   spatial     -- a read or write that leaves the object it started in,
 *                  including one-past-the-end and negative offsets
 *   temporal    -- use after free, double free, and free of a pointer that
 *                  is not an allocation base
 *   uninit      -- a read of bytes inside a live object that were never
 *                  written (tracked per byte, not per object)
 *   wild        -- a dereference of an address in no known object at all
 *
 * Bounds are exact: the table holds the real size, so there are no redzones
 * to size and an overflow is caught at the first byte past the end rather
 * than the first byte past the guard.
 *
 * All state is in fixed static arenas. The runtime never calls malloc for its
 * own bookkeeping, so it cannot recurse through the allocator it is watching
 * and it works on the freestanding targets.
 */
#ifndef CRUST_MEMSAFE_H
#define CRUST_MEMSAFE_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* --- tunables (override with -D before including) ---------------------- */

#ifndef CRUST_MS_MAX_REGIONS
#define CRUST_MS_MAX_REGIONS 65536      /* live + quarantined objects */
#endif
#ifndef CRUST_MS_HASH_SLOTS
#define CRUST_MS_HASH_SLOTS 131072      /* page -> region buckets (pow2) */
#endif
#ifndef CRUST_MS_BITMAP_ARENA
#define CRUST_MS_BITMAP_ARENA (4 * 1024 * 1024)  /* init-shadow bytes */
#endif
#ifndef CRUST_MS_PAGE_SHIFT
#define CRUST_MS_PAGE_SHIFT 12
#endif

/* Region kinds, for the wording of the diagnostic. */
#define CRUST_MS_HEAP   0
#define CRUST_MS_STACK  1
#define CRUST_MS_GLOBAL 2

/* --- lifecycle --------------------------------------------------------- */

/* Idempotent; every entry point calls it, so explicit setup is optional. */
void crust_ms_init(void);

/* Print the run summary (errors seen, and any still-live heap objects when
 * leak reporting is on). Returns a process exit status: 0 clean, 1 dirty.
 * Installed as an atexit handler by crust_ms_init when hosted. */
int crust_ms_report(void);

/* Stop at the first error instead of reporting and continuing. Default off:
 * one test run should surface every distinct bug, not just the first. */
void crust_ms_set_halt_on_error(int on);

/* Whether a run that reported anything overrides the process exit status
 * with 1. On by default: the flag exists to make test suites fail. */
void crust_ms_set_fail_exit(int on);

/* Suppress the leak section of the report (on by default for stack-heavy
 * programs that intentionally leak at exit). */
void crust_ms_set_report_leaks(int on);

/* --- object registration ----------------------------------------------- */

/* Record an object. `initialized` is 1 when every byte already holds a
 * defined value (calloc, a global, a struct assigned at declaration) and 0
 * when the bytes are garbage (malloc, a bare local). `what` is the source
 * spelling used in the diagnostic, e.g. "malloc(16)" or "char buf[8]". */
void crust_ms_register(void *base, size_t size, int kind, int initialized,
                       const char *what, const char *file, int line,
                       const char *func);

/* Retire an object without treating it as a free() -- used for stack frames
 * on scope exit. A later access reports a use-after-scope. */
void crust_ms_unregister(void *base, const char *file, int line,
                         const char *func);

/* --- access checks ------------------------------------------------------ */

/* Both report and return: the caller performs the access regardless, so a
 * single run can surface many errors rather than dying on the first. The
 * return is 1 when the access was clean, 0 when it was reported. */
int crust_ms_check_read(const void *p, size_t n, const char *expr,
                        const char *file, int line, const char *func);
int crust_ms_check_write(void *p, size_t n, const char *expr,
                         const char *file, int line, const char *func);

/* Mark bytes defined without a bounds check -- for memset/memcpy
 * destinations and for the store half of a read-modify-write. */
void crust_ms_mark_init(void *p, size_t n);

/* --- allocator wrappers ------------------------------------------------- */

void *crust_ms_malloc(size_t n, const char *file, int line, const char *func);
void *crust_ms_calloc(size_t nm, size_t n, const char *file, int line,
                      const char *func);
void *crust_ms_realloc(void *p, size_t n, const char *file, int line,
                       const char *func);
void  crust_ms_free(void *p, const char *file, int line, const char *func);

/* --- the macros the instrumented code actually emits --------------------
 *
 * A translation unit compiled without --mem-safe includes this header with
 * CRUST_MEM_SAFE undefined and every macro collapses to the bare expression,
 * so the same generated C is the release build. That is the whole point of
 * the flag: one source, no runtime cost when it is off.
 */

#ifdef CRUST_MEM_SAFE

#define CRUST_MS_RD(p, ty, expr) \
    (crust_ms_check_read((const void *)(p), sizeof(ty), (expr), \
                         __FILE__, __LINE__, CRUST_MS_FUNC), *(p))

#define CRUST_MS_WR(p, ty, expr) \
    (*(ty *)crust_ms_checked_wr((void *)(p), sizeof(ty), (expr), \
                                __FILE__, __LINE__, CRUST_MS_FUNC))

#define CRUST_MS_LOCAL(v, what) \
    crust_ms_register((void *)&(v), sizeof(v), CRUST_MS_STACK, 0, (what), \
                      __FILE__, __LINE__, CRUST_MS_FUNC)
#define CRUST_MS_LOCAL_END(v) \
    crust_ms_unregister((void *)&(v), __FILE__, __LINE__, CRUST_MS_FUNC)

#define CRUST_MS_MALLOC(n)      crust_ms_malloc((n), __FILE__, __LINE__, CRUST_MS_FUNC)
#define CRUST_MS_CALLOC(m, n)   crust_ms_calloc((m), (n), __FILE__, __LINE__, CRUST_MS_FUNC)
#define CRUST_MS_REALLOC(p, n)  crust_ms_realloc((p), (n), __FILE__, __LINE__, CRUST_MS_FUNC)
#define CRUST_MS_FREE(p)        crust_ms_free((p), __FILE__, __LINE__, CRUST_MS_FUNC)

#else  /* release build: every check vanishes */

#define CRUST_MS_RD(p, ty, expr)  (*(p))
#define CRUST_MS_WR(p, ty, expr)  (*(p))
#define CRUST_MS_LOCAL(v, what)   ((void)0)
#define CRUST_MS_LOCAL_END(v)     ((void)0)
#define CRUST_MS_MALLOC(n)        malloc(n)
#define CRUST_MS_CALLOC(m, n)     calloc((m), (n))
#define CRUST_MS_REALLOC(p, n)    realloc((p), (n))
#define CRUST_MS_FREE(p)          free(p)

#endif

/* Checks the write, marks the bytes defined, and hands the pointer back so
 * the store can be an lvalue. Separated out because the macro needs one
 * expression that yields the address. */
void *crust_ms_checked_wr(void *p, size_t n, const char *expr,
                          const char *file, int line, const char *func);

/* __func__ is C99; the generated C targets it, but keep a fallback so the
 * header is usable from hand-written code built with anything. */
/* shivycx implements __func__ (parser/expression.py resolves it, along with
 * the GCC aliases) but does not predefine __STDC_VERSION__, so the usual C99
 * probe alone would silently drop function names from every diagnostic. */
#ifndef CRUST_MS_FUNC
# if defined(__SHIVYC__) || defined(__GNUC__) \
     || (defined(__STDC_VERSION__) && __STDC_VERSION__ >= 199901L)
#  define CRUST_MS_FUNC __func__
# else
#  define CRUST_MS_FUNC ((const char *)0)
# endif
#endif

#ifdef __cplusplus
}
#endif

#endif /* CRUST_MEMSAFE_H */
