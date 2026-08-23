#ifndef _SHIM_LINUX_BITOPS_H
#define _SHIM_LINUX_BITOPS_H
#include <linux/kernel.h>
#include <linux/bits.h>
/* Bitmap operations over an array of unsigned long.
 *
 * Upstream these are atomic (set_bit and friends carry memory barriers and
 * lock prefixes). Here they are plain reads and writes, which is correct for
 * a single-threaded kernel with no interrupts and wrong the moment either
 * changes -- the same caveat GPU.md already records for spinlock.h and
 * ww_mutex.h. Anything built on this inherits it.
 */
static inline void set_bit(unsigned nr, unsigned long *addr)
{
    addr[BIT_WORD(nr)] |= BIT_MASK(nr);
}

static inline void clear_bit(unsigned nr, unsigned long *addr)
{
    addr[BIT_WORD(nr)] &= ~BIT_MASK(nr);
}

static inline void change_bit(unsigned nr, unsigned long *addr)
{
    addr[BIT_WORD(nr)] ^= BIT_MASK(nr);
}

static inline int test_bit(unsigned nr, const unsigned long *addr)
{
    return (addr[BIT_WORD(nr)] & BIT_MASK(nr)) != 0;
}

static inline int test_and_set_bit(unsigned nr, unsigned long *addr)
{
    int old = test_bit(nr, addr);
    set_bit(nr, addr);
    return old;
}

/* The __ prefixed forms are the non-atomic variants upstream. Everything
 * here is already non-atomic, so they are the same functions under both
 * names -- but they must exist under both, because drm_mm calls __set_bit. */
static inline void __set_bit(unsigned nr, unsigned long *addr) { set_bit(nr, addr); }
static inline void __clear_bit(unsigned nr, unsigned long *addr) { clear_bit(nr, addr); }
static inline void __change_bit(unsigned nr, unsigned long *addr) { change_bit(nr, addr); }

static inline int test_and_clear_bit(unsigned nr, unsigned long *addr)
{
    int old = test_bit(nr, addr);
    clear_bit(nr, addr);
    return old;
}

/* The _lock/_unlock forms carry acquire/release ordering upstream. There is
 * no other context to order against here, so they are the plain operations --
 * the same caveat as spinlock.h, and it stops being true the moment there is
 * a second core or an interrupt. */
static inline void clear_bit_unlock(unsigned nr, unsigned long *addr) { clear_bit(nr, addr); }
static inline void set_bit_lock(unsigned nr, unsigned long *addr) { set_bit(nr, addr); }
static inline int test_and_set_bit_lock(unsigned nr, unsigned long *addr)
{
    return test_and_set_bit(nr, addr);
}

/* The bit-scan and population-count helpers below are written as plain C
 * loops rather than as __builtin_ffsll / __builtin_clz / __builtin_popcount.
 *
 * That is deliberate. ShivyCX does not implement those builtins, and since
 * this shim is ours rather than upstream's, depending on them would
 * manufacture a ShivyCX gap out of our own code and report it as though the
 * DRM sources were at fault. gcc recognises these shapes and lowers them to
 * the same instructions anyway.
 *
 * __ffs is undefined for zero, as upstream. ffs is 1-based with 0 meaning
 * "no bits set" -- the opposite convention, and a classic off-by-one. */
static inline unsigned long __ffs(unsigned long word)
{
    unsigned long n = 0;
    while (!(word & 1UL)) { word >>= 1; n++; }
    return n;
}

static inline int ffs(int x)
{
    unsigned int u = (unsigned int)x;
    int n = 1;
    if (!u) return 0;
    while (!(u & 1U)) { u >>= 1; n++; }
    return n;
}

static inline int fls(int x)
{
    unsigned int u = (unsigned int)x;
    int n = 0;
    while (u) { u >>= 1; n++; }
    return n;
}

static inline int fls64(u64 x)
{
    int n = 0;
    while (x) { x >>= 1; n++; }
    return n;
}

static inline unsigned int hweight64(u64 w)
{
    unsigned int n = 0;
    while (w) { n += (unsigned int)(w & 1ULL); w >>= 1; }
    return n;
}
static inline unsigned int hweight32(u32 w) { return hweight64((u64)w); }
static inline unsigned int hweight16(u16 w) { return hweight64((u64)w); }
static inline unsigned int hweight8(u8 w)   { return hweight64((u64)w); }

#define for_each_set_bit(bit, addr, size) \
    for ((bit) = 0; (bit) < (size); (bit)++) if (test_bit((bit), (addr)))
#endif
