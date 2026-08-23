#ifndef _SHIM_LINUX_WORKQUEUE_H
#define _SHIM_LINUX_WORKQUEUE_H
#include <linux/kernel.h>
#include <linux/list.h>
/* Deferred work. Nothing here ever runs it: there is no scheduler and no
 * second context to run it on, so scheduling work is a no-op that returns
 * "already queued" and the callback is never invoked.
 *
 * That is a real behavioural divergence, not just a stub -- upstream, work
 * queued here would eventually execute. drm_mode_config embeds
 * connector_free_work and output_poll_work by value, so the types have to be
 * complete regardless; anything that depends on the work actually running
 * needs a scheduler this tree does not have. */
struct work_struct;
typedef void (*work_func_t)(struct work_struct *work);

struct work_struct {
    struct list_head entry;
    work_func_t      func;
    unsigned long    data;
};

struct delayed_work {
    struct work_struct work;
    unsigned long      expires;
};

struct workqueue_struct { int unused; };

#define INIT_WORK(w, f)         do { (w)->func = (f); } while (0)
#define INIT_DELAYED_WORK(w, f) do { (w)->work.func = (f); } while (0)
#define schedule_work(w)                (0)
#define schedule_delayed_work(w, d)     (0)
#define queue_work(q, w)                (0)
#define queue_delayed_work(q, w, d)     (0)
#define cancel_work_sync(w)             (0)
#define cancel_delayed_work(w)          (0)
#define cancel_delayed_work_sync(w)     (0)
#define flush_work(w)                   (0)
#define flush_workqueue(q)              do { } while (0)
#define destroy_workqueue(q)            do { } while (0)
#define to_delayed_work(w)              container_of(w, struct delayed_work, work)
#endif
