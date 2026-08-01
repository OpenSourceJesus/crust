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
#define BIT(n) (1UL << (n))
#define BIT_ULL(n) (1ULL << (n))
#define DIV_ROUND_UP(n, d) (((n) + (d) - 1) / (d))
#define DIV_ROUND_CLOSEST(n, d) (((n) + (d) / 2) / (d))
#define DIV_ROUND_UP_ULL(n, d) DIV_ROUND_UP(n, d)
#define DIV_ROUND_DOWN_ULL(n, d) ((n) / (d))
#define abs(x) ((x) < 0 ? -(x) : (x))
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
#endif
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
