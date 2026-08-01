/* alloc.c -- the kernel heap.
 *
 * This file owns exactly two things the Rust side cannot: the arena itself,
 * and the translation between a block offset and a real pointer. Every
 * decision about which bytes are in use lives in alloc.rs, where rustc can
 * see it.
 *
 * The arena is a static array rather than memory claimed from the Multiboot
 * memory map. That is deliberate for now: it makes the heap available before
 * anything else is initialised, and it costs nothing but bss. Step 4 brings in
 * the memory map, at which point this becomes the fallback for early boot and
 * the real heap moves to physical memory the bootloader told us about.
 *
 * There is no locking. Nothing here is called from an interrupt handler -- the
 * timer only increments a counter and the keyboard only pushes into a ring
 * that was sized at compile time -- so the foreground has the heap to itself.
 * That has to be revisited the moment step 6 introduces preemption.
 */
#include "mbos.h"
#include "alloc.h"          /* generated from alloc.rs; see gen_rs.py */

#ifndef MBOS_HEAP_BYTES
#define MBOS_HEAP_BYTES (1024 * 1024)
#endif

/* 16-byte aligned so every offset the allocator hands back is aligned too,
 * which is what lets alloc.rs skip leading-pad handling entirely. */
static u8 arena[MBOS_HEAP_BYTES] __attribute__((aligned(16)));

static Heap heap;
static int  heap_ready = 0;

void kheap_init(void) {
    Heap_init(&heap, MBOS_HEAP_BYTES);
    heap_ready = 1;
}

void *kmalloc(size_t n) {
    int off;
    if (!heap_ready) kheap_init();
    if (n > (size_t)0x7FFFFFFF) return 0;

    off = Heap_alloc(&heap, (int)n);
    if (off < 0) return 0;
    return (void *)(arena + off);
}

/* Zeroing allocator. Callers that build a struct field by field do not need
 * it; callers that fill an array lazily do, and getting stale bytes from a
 * recycled block is a bug that reproduces only under memory pressure. */
void *kzalloc(size_t n) {
    void *p = kmalloc(n);
    if (p) mini_memset(p, 0, n);
    return p;
}

void kfree(void *p) {
    long off;
    if (!p) return;                     /* free(NULL) is a no-op, as in libc */
    if (!heap_ready) return;

    off = (long)((u8 *)p - arena);
    if (off < 0 || off >= (long)MBOS_HEAP_BYTES) {
        ser_puts("[mbos] kfree: pointer outside the arena, ignored\n");
        return;
    }
    if (!Heap_free(&heap, (int)off)) {
        /* Not the start of a live block: a double free, or a pointer into the
         * middle of one. Report rather than corrupt -- silently accepting it
         * would merge two live blocks. */
        ser_puts("[mbos] kfree: not a live block, ignored\n");
    }
}

/* ---- introspection, for the `mem` shell command ------------------------ */

size_t kheap_total(void)   { return (size_t)Heap_capacity(&heap); }
size_t kheap_used(void)    { return (size_t)Heap_bytes_used(&heap); }
size_t kheap_largest(void) { return (size_t)Heap_largest_free(&heap); }
int    kheap_blocks(void)  { return Heap_block_count(&heap); }
int    kheap_failures(void){ return Heap_failures(&heap); }
int    kheap_verify(void)  { return Heap_verify(&heap); }

int kheap_block(int i, size_t *off, size_t *size, int *used) {
    if (i < 0 || i >= Heap_block_count(&heap)) return 0;
    *off  = (size_t)Heap_block_off(&heap, i);
    *size = (size_t)Heap_block_size(&heap, i);
    *used = Heap_block_used(&heap, i) ? 1 : 0;
    return 1;
}
