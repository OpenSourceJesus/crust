#ifndef _SHIM_LINUX_BITFIELD_H
#define _SHIM_LINUX_BITFIELD_H
#include <linux/kernel.h>
/* Extract/insert a mask-described field. The kernel version is a wall of
 * build-time checks around exactly this arithmetic. */
#define __bf_shf(x) (__builtin_ffsll(x) - 1)
#define FIELD_GET(mask, val) (((val) & (mask)) >> __bf_shf(mask))
#define FIELD_PREP(mask, val) (((val) << __bf_shf(mask)) & (mask))
#define FIELD_FIT(mask, val) (1)
#define GENMASK(h, l) (((~0UL) << (l)) & (~0UL >> (64 - 1 - (h))))
#define GENMASK_ULL(h, l) GENMASK(h, l)
#endif
