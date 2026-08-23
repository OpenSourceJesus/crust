#ifndef _SHIM_LINUX_IDR_H
#define _SHIM_LINUX_IDR_H
#include <linux/kernel.h>
#include <linux/rbtree.h>
/* An IDR maps small integer ids to pointers. drm_file embeds one by value
 * (drm_file::object_idr), so the struct must be *complete* even though nothing
 * in the portable slice allocates an id -- an autostub placeholder leaves it
 * declared-but-empty and every file reaching drm_file.h fails with
 * "field 'object_idr' has incomplete type". That was the most common blocker
 * in the priority set after lockdep.
 *
 * The layout does not have to match Linux's radix tree, because nothing here
 * inspects it; it only has to have a size. The operations are declared and
 * left undefined so a caller fails at link with a name that says what it
 * wanted, rather than silently getting an id that was never recorded. */
struct idr {
    struct rb_root idr_rt;
    unsigned int   idr_base;
    unsigned int   idr_next;
};

#define DEFINE_IDR(name) struct idr name
#define idr_init(idr)           do { } while (0)
#define idr_init_base(idr, b)   do { } while (0)
#define idr_destroy(idr)        do { } while (0)
#define idr_is_empty(idr)       (1)

int idr_alloc(struct idr *idr, void *ptr, int start, int end, unsigned gfp);
void *idr_find(const struct idr *idr, unsigned long id);
void *idr_remove(struct idr *idr, unsigned long id);
void *idr_replace(struct idr *idr, void *ptr, unsigned long id);

/* Upstream this walks the tree; there is nothing to walk here, so the body
 * never executes. Written as a loop that immediately terminates so callers
 * still compile. */
#define idr_for_each_entry(idr, entry, id) \
    for ((id) = 0, (entry) = NULL; 0; )

struct ida { struct idr ida_rt; };
#define DEFINE_IDA(name) struct ida name
#define ida_init(ida)    do { } while (0)
#define ida_destroy(ida) do { } while (0)
int ida_alloc_range(struct ida *ida, unsigned int min, unsigned int max,
                    unsigned gfp);
void ida_free(struct ida *ida, unsigned int id);
#endif
