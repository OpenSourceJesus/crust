#ifndef _SHIM_LINUX_KREF_H
#define _SHIM_LINUX_KREF_H
#include <linux/kernel.h>
/* Reference counting. drm_device embeds a kref by value, so the type must be
 * complete. The counter is a plain int rather than an atomic: single-threaded
 * with no interrupts, as everywhere else here. */
struct kref { int refcount; };

static inline void kref_init(struct kref *kref) { kref->refcount = 1; }
static inline void kref_get(struct kref *kref) { kref->refcount++; }
static inline unsigned int kref_read(const struct kref *kref)
{
    return (unsigned int)kref->refcount;
}
static inline int kref_put(struct kref *kref, void (*release)(struct kref *))
{
    if (--kref->refcount == 0) {
        release(kref);
        return 1;
    }
    return 0;
}
#endif
