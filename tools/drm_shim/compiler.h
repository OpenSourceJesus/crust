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
#endif
