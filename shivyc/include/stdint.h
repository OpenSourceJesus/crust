#ifndef _STDINT_H
#define _STDINT_H

/* Exact-width integer types. LP64 (Unix) spells the 64-bit types `long`;
 * LLP64 (64-bit Windows) must say `long long`, because there `long` is 4
 * bytes. Crust folds `long long` onto the same 8-byte type as LP64's `long`. */
#ifdef _WIN64
#define __CRUST_I64 long long
#else
#define __CRUST_I64 long
#endif
typedef signed char        int8_t;
typedef short              int16_t;
typedef int                int32_t;
typedef __CRUST_I64        int64_t;

typedef unsigned char      uint8_t;
typedef unsigned short     uint16_t;
typedef unsigned int       uint32_t;
typedef unsigned __CRUST_I64 uint64_t;

/* Pointer-sized integers. */
typedef __CRUST_I64        intptr_t;
typedef unsigned __CRUST_I64 uintptr_t;

/* Greatest-width integers. */
typedef __CRUST_I64        intmax_t;
typedef unsigned __CRUST_I64 uintmax_t;
#undef __CRUST_I64

/* Minimum-width / fastest types (mapped to the exact-width types). */
typedef int8_t             int_least8_t;
typedef int16_t            int_least16_t;
typedef int32_t            int_least32_t;
typedef int64_t            int_least64_t;
typedef uint8_t            uint_least8_t;
typedef uint16_t           uint_least16_t;
typedef uint32_t           uint_least32_t;
typedef uint64_t           uint_least64_t;

typedef int8_t             int_fast8_t;
typedef int64_t            int_fast16_t;
typedef int64_t            int_fast32_t;
typedef int64_t            int_fast64_t;
typedef uint8_t            uint_fast8_t;
typedef uint64_t           uint_fast16_t;
typedef uint64_t           uint_fast32_t;
typedef uint64_t           uint_fast64_t;

/* Limits. */
#define INT8_MAX    0x7f
#define INT16_MAX   0x7fff
#define INT32_MAX   0x7fffffff
#define INT64_MAX   0x7fffffffffffffffLL
#define INT8_MIN    (-INT8_MAX - 1)
#define INT16_MIN   (-INT16_MAX - 1)
#define INT32_MIN   (-INT32_MAX - 1)
#define INT64_MIN   (-INT64_MAX - 1)

#define UINT8_MAX   0xff
#define UINT16_MAX  0xffff
#define UINT32_MAX  0xffffffffU
#define UINT64_MAX  0xffffffffffffffffULL

#define INTPTR_MAX  INT64_MAX
#define INTPTR_MIN  INT64_MIN
#define UINTPTR_MAX UINT64_MAX
#define SIZE_MAX    UINT64_MAX

#endif /* _STDINT_H */
