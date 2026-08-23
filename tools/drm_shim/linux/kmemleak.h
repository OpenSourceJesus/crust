#ifndef _SHIM_LINUX_KMEMLEAK_H
#define _SHIM_LINUX_KMEMLEAK_H
/* Leak tracker. There is none, so every hook is a no-op.
 *
 * This is the other half of the stackdepot story: kmemleak is what wanted
 * depot_stack_handle_t in the first place. Neither records anything here, and
 * neither is called on any path the portable slice exercises. */
#define kmemleak_alloc(p, s, c, g)      do { } while (0)
#define kmemleak_free(p)                do { } while (0)
#define kmemleak_update_trace(p)        do { (void)(p); } while (0)
#define kmemleak_ignore(p)              do { } while (0)
#define kmemleak_not_leak(p)            do { } while (0)
#define kmemleak_no_scan(p)             do { } while (0)
#endif
