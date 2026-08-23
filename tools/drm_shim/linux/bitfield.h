#ifndef _SHIM_LINUX_BITFIELD_H
#define _SHIM_LINUX_BITFIELD_H
#include <linux/kernel.h>
#include <linux/bits.h>
/* Extract/insert a mask-described field. The kernel version is a wall of
 * build-time checks around exactly this arithmetic. */
/* Shift of the lowest set bit in the mask. Plain loop rather than
 * __builtin_ffsll, which ShivyCX does not implement -- see bitops.h. */
#define __bf_shf(x) (__bf_shf_fn((unsigned long long)(x)))
static inline unsigned __bf_shf_fn(unsigned long long m)
{
    unsigned n = 0;
    if (!m) return 0;
    while (!(m & 1ULL)) { m >>= 1; n++; }
    return n;
}
#define FIELD_GET(mask, val) (((val) & (mask)) >> __bf_shf(mask))
#define FIELD_PREP(mask, val) (((val) << __bf_shf(mask)) & (mask))
#define FIELD_FIT(mask, val) (1)
/* GENMASK/GENMASK_ULL live in bits.h; see the note in kernel.h. */
#endif
