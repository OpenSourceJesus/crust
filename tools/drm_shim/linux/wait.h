#ifndef _SHIM_LINUX_WAIT_H
#define _SHIM_LINUX_WAIT_H
#include <linux/kernel.h>
#include <linux/list.h>
/* Wait queues. Nothing can block here: there is no scheduler, so there is no
 * other context to wake and no way to yield to it.
 *
 * That makes wait_event() a hard divergence rather than a stub. Upstream it
 * sleeps until the condition holds; here it evaluates the condition once and
 * continues regardless. Any caller whose correctness depends on actually
 * waiting is silently broken -- which is acceptable only because nothing in
 * the portable slice waits; drm_mode_config merely embeds the queue by value
 * for the vblank path, which is not compiled here. */
struct wait_queue_head {
    spinlock_t       lock;
    struct list_head head;
};
typedef struct wait_queue_head wait_queue_head_t;

struct wait_queue_entry {
    unsigned int     flags;
    void            *private;
    struct list_head entry;
};
typedef struct wait_queue_entry wait_queue_entry_t;

#define init_waitqueue_head(q)          do { INIT_LIST_HEAD(&(q)->head); } while (0)
#define DECLARE_WAIT_QUEUE_HEAD(name)   wait_queue_head_t name
#define wake_up(q)                      do { (void)(q); } while (0)
#define wake_up_all(q)                  do { (void)(q); } while (0)
#define wake_up_interruptible(q)        do { (void)(q); } while (0)
#define waitqueue_active(q)             (0)
#define wait_event(q, cond)             do { (void)(q); (void)(cond); } while (0)
#define wait_event_timeout(q, cond, t)  ((cond) ? 1 : 0)
#define wait_event_interruptible(q, cond) ((cond) ? 0 : 0)
#define add_wait_queue(q, e)            do { } while (0)
#define remove_wait_queue(q, e)         do { } while (0)
#endif
