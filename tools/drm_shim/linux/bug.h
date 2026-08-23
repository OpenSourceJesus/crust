#ifndef _SHIM_LINUX_BUG_H
#define _SHIM_LINUX_BUG_H
#include <linux/kernel.h>
/* Assertions. All of these evaluate their condition and then do nothing with
 * it, which is what a freestanding build with no console panic path can
 * honestly offer -- but note the difference from upstream: there, BUG_ON
 * halts. Here execution continues with whatever invariant was violated, so a
 * bug that upstream would stop dead propagates instead. */
#define BUG()                       do { } while (0)
#define BUG_ON(c)                   do { (void)(c); } while (0)
#define WARN(c, ...)                (!!(c))
#define WARN_ONCE(c, ...)           (!!(c))
#define WARN_ON_ONCE(c)             (!!(c))
/* Compile-time checks. BUILD_BUG_ON can be real: a negative-width bitfield is
 * rejected at compile time exactly as upstream intends. */
#define BUILD_BUG_ON(c)             ((void)sizeof(struct { int _[(c) ? -1 : 1]; }))
#define BUILD_BUG_ON_INVALID(e)     ((void)(0 && (e)))
#define BUILD_BUG_ON_MSG(c, m)      BUILD_BUG_ON(c)
#define BUILD_BUG_ON_ZERO(c)        (0)
#endif
