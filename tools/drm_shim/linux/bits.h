#ifndef _SHIM_LINUX_BITS_H
#define _SHIM_LINUX_BITS_H
/* Bit and mask construction. Upstream this lives in <linux/bits.h> and is
 * pulled in by almost everything; our bitfield.h also defines GENMASK, which
 * is why drm_buddy.c could fail on GENMASK_ULL while the shim "had" it --
 * nothing drm_buddy includes reaches bitfield.h.
 *
 * Deliberately no <linux/kernel.h> include: kernel.h includes *this*, and the
 * dependency has to run one way.
 *
 * GENMASK(h, l) is the inclusive mask from bit l to bit h. The kernel wraps
 * these in build-time checks that h >= l; we cannot, so a reversed pair gives
 * a nonsense mask here where upstream would refuse to build. */
#define BITS_PER_LONG 64
#define BITS_PER_LONG_LONG 64
#define BITS_PER_BYTE 8

#define BIT(n)      (1UL << (n))
#define BIT_ULL(n)  (1ULL << (n))
#define BIT_MASK(nr)      (1UL << ((nr) % BITS_PER_LONG))
#define BIT_WORD(nr)      ((nr) / BITS_PER_LONG)
#define BITS_TO_LONGS(nr) (((nr) + BITS_PER_LONG - 1) / BITS_PER_LONG)

#define GENMASK(h, l) \
    (((~0UL) - (1UL << (l)) + 1) & (~0UL >> (BITS_PER_LONG - 1 - (h))))
#define GENMASK_ULL(h, l) \
    (((~0ULL) - (1ULL << (l)) + 1) & (~0ULL >> (BITS_PER_LONG_LONG - 1 - (h))))
#endif
