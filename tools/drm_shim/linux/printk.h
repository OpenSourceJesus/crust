#ifndef _SHIM_LINUX_PRINTK_H
#define _SHIM_LINUX_PRINTK_H
#include <linux/kernel.h>

/* Diagnostics go nowhere in a freestanding build. DRM sprays these through the
 * generic helpers, and none of them affect what the algorithms compute, so the
 * whole family collapses to nothing. A kernel that wants the messages can
 * point them at its own console instead. */
#define pr_info(...)        do { } while (0)
#define pr_err(...)         do { } while (0)
#define pr_warn(...)        do { } while (0)
#define pr_debug(...)       do { } while (0)
#define printk(...)         do { } while (0)

#define DRM_DEBUG(...)      do { } while (0)
#define DRM_DEBUG_KMS(...)  do { } while (0)
#define DRM_DEBUG_DRIVER(...) do { } while (0)
#define DRM_DEBUG_ATOMIC(...) do { } while (0)
#define DRM_ERROR(...)      do { } while (0)
#define DRM_WARN(...)       do { } while (0)
#define DRM_INFO(...)       do { } while (0)
#define DRM_NOTE(...)       do { } while (0)

#define drm_dbg(...)        do { } while (0)
#define drm_dbg_kms(...)    do { } while (0)
#define drm_dbg_atomic(...) do { } while (0)
#define drm_dbg_core(...)   do { } while (0)
#define drm_err(...)        do { } while (0)
#define drm_warn(...)       do { } while (0)
#define drm_info(...)       do { } while (0)
#define drm_WARN(...)       (0)
#define drm_WARN_ON(...)    (0)
#define drm_WARN_ONCE(...)  (0)
#define WARN_ONCE(c, ...)   (!!(c))
#define BUG_ON(c)           do { } while (0)
#endif
