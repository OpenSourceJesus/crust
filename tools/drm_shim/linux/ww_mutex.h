#ifndef _SHIM_LINUX_WW_MUTEX_H
#define _SHIM_LINUX_WW_MUTEX_H
#include <linux/kernel.h>
/* Wound/wait mutexes order multi-object locking. Single-threaded here, so the
 * context is an empty token that still has to be a complete type: DRM embeds
 * it by value in drm_modeset_acquire_state. */
struct ww_class { int unused; };
struct ww_acquire_ctx { int unused; };
struct mutex { int unused; };
/* DRM reaches into ww_mutex::base for the plain mutex underneath. */
struct ww_mutex { struct mutex base; };
#define ww_mutex_init(l, c) do { } while (0)
#define ww_mutex_lock(l, c) (0)
#define ww_mutex_unlock(l) do { } while (0)
#define ww_acquire_init(c, cl) do { } while (0)
#define ww_acquire_fini(c) do { } while (0)
#define ww_acquire_done(c) do { } while (0)
/* Always false: nothing is ever locked here. Only surfaced once the oracle
 * stopped accepting implicit declarations -- drm_edid.c calls it inside a
 * lock assertion. */
#define ww_mutex_is_locked(l) (0)
#include <linux/list.h>
#include <linux/string.h>
#endif
