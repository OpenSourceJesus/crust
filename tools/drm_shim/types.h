#ifndef _SHIM_LINUX_TYPES_H
#define _SHIM_LINUX_TYPES_H
/* The kernel's fixed-width spellings, as used by the uapi headers. */
#include <linux/kernel.h>
#include <linux/compiler.h>
typedef signed char __s8;        typedef unsigned char __u8;
typedef short __s16;             typedef unsigned short __u16;
typedef int __s32;               typedef unsigned int __u32;
typedef long long __s64;         typedef unsigned long long __u64;
typedef unsigned long __kernel_size_t;
/* the stdint spellings, which the uapi headers use alongside the __u* ones */
typedef signed char int8_t;      typedef unsigned char uint8_t;
typedef short int16_t;           typedef unsigned short uint16_t;
typedef int int32_t;             typedef unsigned int uint32_t;
typedef long long int64_t;       typedef unsigned long long uint64_t;
typedef unsigned long long __aligned_u64;
typedef unsigned int __aligned_u32;
typedef unsigned long dma_addr_t;
typedef long long loff_t;
typedef int pid_t;
typedef unsigned int uid_t;
typedef unsigned long resource_size_t;
/* Endian-annotated types. x86 is little-endian, so these are the plain ones
 * and the le*_to_cpu helpers are identities. */
typedef unsigned short __le16;   typedef unsigned short __be16;
typedef unsigned int __le32;     typedef unsigned int __be32;
typedef unsigned long long __le64; typedef unsigned long long __be64;
#define le16_to_cpu(x) (x)
#define le32_to_cpu(x) (x)
#define le64_to_cpu(x) (x)
#define cpu_to_le16(x) (x)
#define cpu_to_le32(x) (x)
#define cpu_to_le64(x) (x)
typedef struct { int counter; } atomic_t;
#endif
