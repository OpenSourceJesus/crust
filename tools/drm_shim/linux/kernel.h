#ifndef _SHIM_LINUX_KERNEL_H
#define _SHIM_LINUX_KERNEL_H
/* Minimal freestanding stand-in for <linux/kernel.h>: just the type names and
 * helper macros the portable DRM algorithm files actually use. */
typedef signed char s8;      typedef unsigned char u8;
typedef short s16;           typedef unsigned short u16;
typedef int s32;             typedef unsigned int u32;
typedef long s64;            typedef unsigned long u64;
typedef unsigned long size_t;
typedef _Bool bool;
typedef long ssize_t;
typedef long ptrdiff_t;
typedef __builtin_va_list va_list;
struct device;
struct va_format { const char *fmt; va_list *va; };
struct drm_device;
#define true 1
#define false 0
#define NULL ((void *)0)
#define max(a, b) ((a) > (b) ? (a) : (b))
#define min(a, b) ((a) < (b) ? (a) : (b))
#define clamp(v, lo, hi) min(max(v, lo), hi)
#define swap(a, b) do { typeof(a) __t = (a); (a) = (b); (b) = __t; } while (0)
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
/* BIT/BIT_ULL/GENMASK come from bits.h, which is where the sources expect
 * them. They were previously defined here as well; two definitions of the
 * same macro in different headers is how drm_buddy.c managed to fail on
 * GENMASK_ULL while the shim appeared to provide it. */
#include <linux/bits.h>
/* Lock assertions. Upstream these arrive via mutex.h / spinlock.h, which are
 * autostubs here, so they are pulled in centrally instead. */
#include <linux/lockdep.h>
#define DIV_ROUND_UP(n, d) (((n) + (d) - 1) / (d))
#define DIV_ROUND_CLOSEST(n, d) (((n) + (d) / 2) / (d))
#define DIV_ROUND_UP_ULL(n, d) DIV_ROUND_UP(n, d)
#define DIV_ROUND_DOWN_ULL(n, d) ((n) / (d))
#define abs(x) ((x) < 0 ? -(x) : (x))
/* Alignment and rounding. round_down/round_up require a power-of-two second
 * argument, as upstream; roundup/rounddown do not and use a division. */
#define ALIGN(x, a)          (((x) + (a) - 1) & ~((typeof(x))(a) - 1))
#define ALIGN_DOWN(x, a)     ((x) & ~((typeof(x))(a) - 1))
#define IS_ALIGNED(x, a)     (((x) & ((typeof(x))(a) - 1)) == 0)
#define PTR_ALIGN(p, a)      ((typeof(p))ALIGN((unsigned long)(p), (a)))
#define round_up(x, y)       ((((x) - 1) | ((typeof(x))(y) - 1)) + 1)
#define round_down(x, y)     ((x) & ~((typeof(x))(y) - 1))
#define roundup(x, y)        ((((x) + (y) - 1) / (y)) * (y))
#define rounddown(x, y)      (((x) / (y)) * (y))
#define min_t(t, a, b)       ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define max_t(t, a, b)       ((t)(a) > (t)(b) ? (t)(a) : (t)(b))
#define clamp_t(t, v, lo, hi) min_t(t, max_t(t, v, lo), hi)
#define clamp_val(v, lo, hi) clamp(v, lo, hi)
#define offsetof(t, m) __builtin_offsetof(t, m)
#define container_of(p, t, m) \
    ((t *)((char *)(p) - offsetof(t, m)))
/* 64-bit helpers the timing and scaling math uses. On x86-64 these are plain
 * arithmetic; the kernel spells them out because 32-bit targets cannot. */
static inline u64 mul_u32_u32(u32 a, u32 b) { return (u64)a * (u64)b; }
static inline u64 div64_u64(u64 a, u64 b) { return b ? a / b : 0; }
static inline u64 div_u64(u64 a, u32 b) { return b ? a / b : 0; }
static inline s64 div64_s64(s64 a, s64 b) { return b ? a / b : 0; }
#define WARN_ON(c) (!!(c))
#define EXPORT_SYMBOL(s)
#define EXPORT_SYMBOL_GPL(s)
/* limits the allocators compare against */
#define U8_MAX   0xffU
#define U16_MAX  0xffffU
#define U32_MAX  0xffffffffU
#define U64_MAX  0xffffffffffffffffULL
#define S32_MAX  0x7fffffff
#define S32_MIN  (-S32_MAX - 1)
#define S64_MAX  0x7fffffffffffffffLL
#define S64_MIN  (-S64_MAX - 1)
#define INT_MAX  S32_MAX
#define INT_MIN  S32_MIN
#define UINT_MAX U32_MAX
#define LONG_MAX S64_MAX
#include <linux/errno.h>
#define _THIS_IP_ 0UL
#define _RET_IP_ 0UL

/* always available: gcc has these as builtins, ShivyCX needs the prototypes */
#include <linux/string.h>

/* These must come last, after the scalar typedefs above, because each of them
 * includes this header back and will get an empty file when the guard is
 * already set -- so anything they need from here must already be defined.
 *
 * They are pulled in centrally rather than left to each source because the
 * DRM headers rely on the kernel's habit of transitive inclusion:
 * drm_mode_config.h names spinlock_t, delayed_work and kref without including
 * anything that defines them, and drm_device embeds all three by value. */
#include <linux/spinlock.h>
#include <linux/workqueue.h>
#include <linux/kref.h>
#include <linux/bitops.h>
#include <linux/wait.h>
#include <linux/log2.h>
#include <linux/sched.h>
#include <linux/kgdb.h>
#include <linux/err.h>

/* NB: this guard must close at the very end of the file. It previously closed
 * above the limits block, leaving the errno.h and string.h includes outside
 * it -- which turned one missing header into five identical diagnostics, and
 * into unbounded include recursion once an autostub placeholder for errno.h
 * (which itself includes this file) existed. */
#endif
