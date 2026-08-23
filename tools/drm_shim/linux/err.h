#ifndef _SHIM_LINUX_ERR_H
#define _SHIM_LINUX_ERR_H
#include <linux/kernel.h>
/* The kernel's error-in-a-pointer convention: a small negative value cast to
 * a pointer, distinguishable from a real address because no valid kernel
 * object lives in the last page.
 *
 * These must be real definitions rather than an autostub placeholder, and the
 * reason is worth recording. Left undeclared, gcc assumes `int ERR_PTR()` and
 * truncates the returned pointer to 32 bits -- a warning it suppresses under
 * -w, so the file appears to compile while generating wrong code. ShivyCX
 * refuses instead, which is the better behaviour and was briefly mistaken for
 * a compiler gap.
 *
 * MAX_ERRNO is 4095 in Linux; the comparison is unsigned so that any value in
 * the top page reads as an error. */
#define MAX_ERRNO 4095
#define IS_ERR_VALUE(x) ((unsigned long)(void *)(x) >= (unsigned long)-MAX_ERRNO)

static inline void *ERR_PTR(long error) { return (void *)error; }
static inline long PTR_ERR(const void *ptr) { return (long)ptr; }
static inline bool IS_ERR(const void *ptr) { return IS_ERR_VALUE((unsigned long)ptr); }
static inline bool IS_ERR_OR_NULL(const void *ptr)
{
    return !ptr || IS_ERR_VALUE((unsigned long)ptr);
}
static inline void *ERR_CAST(const void *ptr) { return (void *)ptr; }
static inline int PTR_ERR_OR_ZERO(const void *ptr)
{
    return IS_ERR(ptr) ? (int)PTR_ERR(ptr) : 0;
}
#endif
