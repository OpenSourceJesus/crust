#ifndef _SHIM_LINUX_LOG2_H
#define _SHIM_LINUX_LOG2_H
#include <linux/kernel.h>
/* Deliberately does NOT include bitops.h. kernel.h includes both, and a
 * source that includes bitops.h *first* would re-enter kernel.h, find the
 * bitops guard already set, and reach this header before bitops' function
 * bodies exist -- so fls() would be an implicit declaration here and a
 * conflicting static one there. Computing the log directly costs a few lines
 * and removes the ordering dependency entirely. */
/* Power-of-two arithmetic. drm_buddy.c is built entirely around it: block
 * sizes, the minimum order, and the split/merge arithmetic all assume exact
 * powers of two.
 *
 * is_power_of_2(0) is false, matching upstream -- zero has no order, and
 * treating it as a power of two would make the buddy allocator's order
 * calculation loop. */
static inline bool is_power_of_2(unsigned long n)
{
    return n != 0 && (n & (n - 1)) == 0;
}

static inline unsigned long __roundup_pow_of_two(unsigned long n)
{
    unsigned long p = 1;
    while (p < n) p <<= 1;
    return p;
}

static inline unsigned long __rounddown_pow_of_two(unsigned long n)
{
    unsigned long p = 1;
    while ((p << 1) && (p << 1) <= n) p <<= 1;
    return p;
}

#define roundup_pow_of_two(n)   __roundup_pow_of_two(n)
#define rounddown_pow_of_two(n) __rounddown_pow_of_two(n)

static inline int __ilog2_u64(u64 n)
{
    int b = -1;
    while (n) { n >>= 1; b++; }
    return b;
}
static inline int __ilog2_u32(u32 n) { return __ilog2_u64((u64)n); }
#define ilog2(n) ((sizeof(n) <= 4) ? __ilog2_u32((u32)(n)) : __ilog2_u64((u64)(n)))
#define order_base_2(n) ilog2(roundup_pow_of_two(n))
#endif
