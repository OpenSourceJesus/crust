#ifndef _STDDEF_H
#define _STDDEF_H

#ifdef _WIN64
/* 64-bit Windows is LLP64: `long` is 4 bytes, `long long` is 8. */
typedef unsigned long long size_t;
typedef long long ptrdiff_t;
#else
typedef unsigned long size_t;
typedef long ptrdiff_t;
#endif

#ifndef NULL
#define NULL ((void *)0)
#endif

#define offsetof(type, member) __builtin_offsetof(type, member)

#endif /* _STDDEF_H */
