#ifndef _SHIM_COMPILER_H
#define _SHIM_COMPILER_H
/* Attribute macros the DRM headers decorate declarations with. Freestanding
 * builds do not need any of them to mean anything. */
#define __printf(a, b)
#define __scanf(a, b)
#define __always_inline inline
#define __maybe_unused
#define __must_check
#define __user
#define __iomem
#define __force
#define __init
#define __exit
#define __read_mostly
#define __packed __attribute__((packed))
#define __aligned(x) __attribute__((aligned(x)))
#define likely(x) (x)
#define unlikely(x) (x)
#define barrier() do { } while (0)
/* READ_ONCE/WRITE_ONCE stop the compiler from tearing or refetching an access
 * that another context could change under it. Single-threaded with no
 * interrupts, nothing else can, so the volatile cast is all that is needed --
 * and even that is kept rather than dropped, because it costs nothing and
 * keeps the access shape identical to upstream's. */
#define READ_ONCE(x)      (*(const volatile typeof(x) *)&(x))
#define WRITE_ONCE(x, v)  do { *(volatile typeof(x) *)&(x) = (v); } while (0)
#define smp_rmb()         do { } while (0)
#define smp_wmb()         do { } while (0)
#define smp_mb()          do { } while (0)
#define __rcu
#define __percpu
#define __cold
#define __pure
#define __weak            __attribute__((weak))
#define __noreturn        __attribute__((noreturn))
#define fallthrough       do { } while (0)
#endif
