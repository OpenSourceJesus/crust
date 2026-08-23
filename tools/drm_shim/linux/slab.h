#ifndef _SHIM_LINUX_SLAB_H
#define _SHIM_LINUX_SLAB_H
#include <linux/kernel.h>
#include <linux/bitops.h>
/* Allocation. Declared, not defined: the freestanding runtime supplies these
 * (mbos: libmini, CrustOS: rlibc), which is the arrangement GPU.md already
 * records for kmalloc/kfree.
 *
 * The GFP flags are placeholders. Nothing here can sleep, reclaim or use
 * high memory, so the values are never inspected -- but they must exist,
 * because the sources pass them by name.
 *
 * bitops.h is included here rather than left to the caller because drm_mm.c
 * reaches test_bit through this header, not through <linux/bitops.h>
 * directly. */
#define GFP_KERNEL   0x0000U
#define GFP_ATOMIC   0x0001U
#define GFP_NOWAIT   0x0002U
#define GFP_DMA      0x0004U
#define GFP_USER     0x0008U
#define __GFP_ZERO   0x0100U
#define __GFP_NOWARN 0x0200U
#define __GFP_RETRY_MAYFAIL 0x0400U

typedef unsigned int gfp_t;

void *kmalloc(size_t size, gfp_t flags);
void *kzalloc(size_t size, gfp_t flags);
void *kcalloc(size_t n, size_t size, gfp_t flags);
void *kmalloc_array(size_t n, size_t size, gfp_t flags);
void *krealloc(const void *p, size_t new_size, gfp_t flags);
void kfree(const void *p);
void *vmalloc(unsigned long size);
void *vzalloc(unsigned long size);
void vfree(const void *p);

/* Slab caches collapse to plain allocation: one cache, no reuse, no
 * constructor. Correct in the sense that callers get zeroed memory of the
 * right size; wrong if anything depends on cache identity or on the
 * constructor running. Nothing in the portable slice does. */
struct kmem_cache { size_t obj_size; };

struct kmem_cache *kmem_cache_create(const char *name, size_t size,
                                     size_t align, unsigned long flags,
                                     void (*ctor)(void *));
void kmem_cache_destroy(struct kmem_cache *c);
void *kmem_cache_alloc(struct kmem_cache *c, gfp_t flags);
void *kmem_cache_zalloc(struct kmem_cache *c, gfp_t flags);
void kmem_cache_free(struct kmem_cache *c, void *obj);

/* Upstream derives the cache name and size from the struct itself, and its
 * alignment via __alignof__. ShivyCX does not implement __alignof__ on a type,
 * and since this macro is ours rather than upstream's, depending on it would
 * manufacture a ShivyCX gap out of shim code -- the same mistake the
 * __builtin_ffsll usage made earlier. Pointer alignment is a safe upper bound
 * for every struct these caches hold. */
#define KMEM_CACHE(struct_name, flags) \
    kmem_cache_create(#struct_name, sizeof(struct struct_name), \
                      sizeof(void *), (flags), NULL)
#endif
