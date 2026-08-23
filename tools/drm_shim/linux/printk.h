#ifndef _SHIM_LINUX_PRINTK_H
#define _SHIM_LINUX_PRINTK_H
#include <linux/kernel.h>

/* Diagnostics go nowhere in a freestanding build. None of them affect what the
 * algorithms compute, so the whole family collapses to nothing. A kernel that
 * wants the messages can point them at its own console instead.
 *
 * Scope note: this header defines only the *Linux* printk API. It previously
 * also defined drm_info, drm_warn, drm_err, drm_WARN_ON and the DRM_* family,
 * which are DRM's namespace rather than Linux's -- and since drm_print.h is
 * included after this header, upstream's real definitions won and ours were
 * dead code that gcc reported as a redefinition on every file. Anything named
 * drm_* belongs to <drm/drm_print.h>, which the vendor tree supplies.
 *
 * BUG_ON and the WARN family are likewise not here; they live in bug.h. */
#define pr_info(...)        do { } while (0)
#define pr_err(...)         do { } while (0)
#define pr_warn(...)        do { } while (0)
#define pr_notice(...)      do { } while (0)
#define pr_debug(...)       do { } while (0)
#define pr_cont(...)        do { } while (0)
#define pr_info_once(...)   do { } while (0)
#define pr_warn_once(...)   do { } while (0)
#define printk(...)         do { } while (0)
#define no_printk(...)      do { } while (0)

#define KERN_EMERG   ""
#define KERN_ALERT   ""
#define KERN_CRIT    ""
#define KERN_ERR     ""
#define KERN_WARNING ""
#define KERN_NOTICE  ""
#define KERN_INFO    ""
#define KERN_DEBUG   ""
#define KERN_CONT    ""
#endif
